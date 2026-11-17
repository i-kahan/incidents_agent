"""Shared preflight, execution and scoring for evaluation commands."""

import argparse
import dataclasses
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from threading import Lock

from incidents_agent.agent import config as agent_config
from incidents_agent.agent import fake_policy
from incidents_agent.agent import state
from incidents_agent.eval import agent_adapter
from incidents_agent.eval import answers
from incidents_agent.eval import baseline_reference
from incidents_agent.eval import field
from incidents_agent.eval import memory_seed
from incidents_agent.eval import reference_agent
from incidents_agent.eval import reporting
from incidents_agent.eval import retry
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import selection
from incidents_agent.eval import single_call
from incidents_agent.eval import split
from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger
from incidents_agent.llm import litellm_transport
from incidents_agent.llm import pricing

ROOT = Path(__file__).resolve().parents[3]


@dataclasses.dataclass(frozen=True)
class Options:
    """One configuration shared by both evaluation entrypoints."""

    cases: str = 'tuning'
    model: str | None = None
    agent: str = 'single'
    mode: str | None = None
    limit: int | None = None
    workers: int = 1
    dry_run: bool = False
    final: bool = False
    repeat_id: str = 'default'
    resume: Path | None = None
    config: Path = ROOT / 'config/agent.yaml'
    split: Path = ROOT / 'splits/re2-ob.json'
    tasks_root: Path | None = None
    answers: Path | None = None
    field_root: Path = ROOT / 'evalsets/field'
    output_root: Path | None = None
    baseline_root: Path | None = None
    memory: bool = False
    memory_path: Path | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Options':
        """Read only shared options from a script's parsed arguments."""
        return cls(
            **{
                field.name: getattr(args, field.name)
                for field in dataclasses.fields(cls)
            }
        )


@dataclasses.dataclass(frozen=True)
class Prepared:
    """Validated inputs and executor, with no model calls made yet."""

    options: Options
    cases: tuple[runner.Case, ...]
    manifest: dict[str, object]
    output: Path
    baseline_root: Path
    tasks_root: Path
    answers_path: Path
    executor: runner.Execute
    spend: ledger.SpendLedger | None
    snapshot: memory_seed.FrozenSnapshot | None = None
    reserve: str | None = None


@dataclasses.dataclass(frozen=True)
class Evaluation:
    """Saved artifacts and typed scores from one evaluation."""

    folder: Path
    manifest: dict[str, object]
    records: list[dict[str, object]]
    summary: dict[str, object]
    scored: scoring.ScoredRun | None
    usage: dict[str, object]
    invocation_cost_usd: float


def source_fingerprint(root: Path) -> str:
    """Hash output-affecting code and locked dependencies, not report prose."""
    inputs = {}
    for part in ('agent', 'tools', 'ingest', 'llm', 'memory'):
        for path in sorted(
            (root / 'src' / 'incidents_agent' / part).glob('*.py')
        ):
            inputs[str(path.relative_to(root))] = path.read_text()
    paths = ['uv.lock'] + [
        f'src/incidents_agent/eval/{name}.py'
        for name in (
            'agent_adapter',
            'single_call',
            'retry',
            'runner',
            'telemetry_summary',
            'memory_seed',
        )
    ]
    for relative in paths:
        inputs[relative] = (root / relative).read_text()
    return runner.digest(inputs)


def shared_root() -> Path:
    """Locate the common repository for one cache and spend ledger."""
    common = subprocess.check_output(
        ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        cwd=ROOT,
        text=True,
    ).strip()
    return Path(common).parent


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared single-configuration command options."""
    parser.add_argument('--cases', default='tuning')
    parser.add_argument('--model')
    parser.add_argument(
        '--agent', choices=('single', 'single_call'), default='single'
    )
    parser.add_argument(
        '--mode',
        choices=agent_config.MODES,
        help='Agent architecture (Phase 07); default: the agent config',
    )
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--repeat-id', default='default')
    parser.add_argument('--resume', type=Path)
    parser.add_argument(
        '--config', type=Path, default=ROOT / 'config/agent.yaml'
    )
    parser.add_argument(
        '--split', type=Path, default=ROOT / 'splits/re2-ob.json'
    )
    parser.add_argument('--tasks-root', type=Path)
    parser.add_argument('--answers', type=Path)
    parser.add_argument(
        '--field-root', type=Path, default=ROOT / 'evalsets/field'
    )
    parser.add_argument('--output-root', type=Path, help='Dry-run output root')
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument(
        '--memory-path', type=Path, help='Frozen bundle from seed_memory'
    )


def prepare(args: Options, *, release_arm: str | None = None) -> Prepared:
    """Validate and freeze inputs before any paid calls."""
    shared = shared_root()
    baseline_root = args.baseline_root or shared
    tasks_root = args.tasks_root or shared / 'agent_tasks/re2-ob'
    output = args.output_root or shared / 'runs/phase-06'
    partition = split.from_json(json.loads(args.split.read_text()))
    ids = selection.resolve(
        args.cases,
        partition,
        final=args.final,
        limit=args.limit,
        field_ids=field.CASE_IDS,
    )
    heldout = bool(set(ids) & set(partition.heldout))
    if heldout and not args.dry_run and release_arm is None:
        raise ValueError('Paid held-out cases require scripts.final_eval')
    use_reserve = (
        heldout
        and args.final
        and release_arm
        not in (
            'memory',
            'multi',
            'rcaeval-reference',
        )
    )
    if args.workers < 1:
        raise ValueError('workers must be positive')
    is_reference = release_arm == 'rcaeval-reference'
    if args.agent not in ('single', 'single_call'):
        raise ValueError('Unknown agent mode')
    if args.agent == 'single_call' and args.cases == 'field':
        raise ValueError('Single-call baseline supports RCAEval only')
    settings = agent_config.load_agent_config(args.config)
    if args.model:
        settings = dataclasses.replace(settings, model_alias=args.model)
    if args.mode is not None:
        settings = dataclasses.replace(
            settings, mode=agent_config.parse_mode(args.mode)
        )
    if args.agent == 'single_call' and settings.mode == 'multi':
        raise ValueError('The single-call baseline has no multi mode')
    memory_enabled = args.memory or settings.memory
    if args.memory_path is not None and not memory_enabled:
        raise ValueError('--memory-path requires memory to be enabled')
    if memory_enabled and args.memory_path is None:
        raise ValueError(
            'Enabled memory requires --memory-path to a seed bundle'
        )
    if memory_enabled and args.agent == 'single_call':
        raise ValueError('Single-call baseline does not support memory')
    settings = dataclasses.replace(
        settings, memory=memory_enabled, memory_frozen=memory_enabled
    )
    snapshot = (
        memory_seed.capture(args.memory_path, partition, ids)
        if memory_enabled and args.memory_path is not None
        else None
    )
    suite = 'field' if args.cases == 'field' else 'rcaeval'
    base = args.field_root if suite == 'field' else tasks_root
    cases = [
        runner.Case(case_id, base / 'cases' / case_id, suite) for case_id in ids
    ]
    if output.resolve().is_relative_to(base.resolve()):
        raise ValueError(
            'Evaluation output must be outside the blind task view'
        )
    answers_path = args.answers or shared / 'agent_tasks' / (
        'tuning.answers.jsonl'
        if set(ids) <= set(partition.tuning)
        else 're2-ob.answers.jsonl'
    )
    if suite == 'rcaeval':
        if answers_path.resolve().is_relative_to(tasks_root.resolve()):
            raise ValueError('Answers must be outside the blind task view')
        answers.subset(answers.load_answers(answers_path), ids)
        reference = baseline_root / 'results/phase-02/reference_baseline.json'
        if reference.exists():
            try:
                baseline_reference.validate(
                    reference, baseline_root / 'runs/phase-02', args.split
                )
            except (OSError, ValueError) as error:
                raise ValueError(
                    f'Invalid baseline reference. Run make baselines. {error}'
                ) from error
    models = config.load_models(ROOT / 'config/models.yaml')
    alias = args.model or settings.model_alias or models.default
    if alias not in models.aliases:
        raise ValueError(f'Unknown model alias: {alias}')
    settings = dataclasses.replace(settings, model_alias=alias)
    spend: ledger.SpendLedger | None = None
    resolved: dict[str, object] = {'model': 'rule-fake'}
    if args.dry_run:

        def make_client(task: state.Task) -> contracts.ModelClient:
            if args.agent == 'single_call':
                return single_call.fake_client(task)
            return fake_policy.RuleClient(task.candidate_services)
    else:
        if args.split.resolve() != (ROOT / 'splits/re2-ob.json').resolve():
            raise ValueError('Paid runs require the repository locked split')
        if output.resolve() != (shared / 'runs/phase-06').resolve():
            raise ValueError('Paid runs use the shared evaluation cache')
        if (
            subprocess.check_output(
                ['git', 'branch', '--show-current'], cwd=ROOT, text=True
            ).strip()
            != 'main'
        ):
            raise ValueError('Paid evaluations run only from merged main')
        if subprocess.check_output(
            [
                'git',
                'status',
                '--porcelain',
                '--',
                'src',
                'scripts',
                'config',
                'splits',
                'uv.lock',
                'pyproject.toml',
            ],
            cwd=ROOT,
            text=True,
        ).strip():
            raise ValueError(
                'Paid evaluation requires a clean tracked checkout'
            )
        env = config.environment(shared / '.env')
        models = config.load_models(ROOT / 'config/models.yaml')
        alias = args.model or settings.model_alias or models.default
        info = client.model_info(models, env, pricing.bundled_catalog(), alias)
        if not info.usable:
            raise ValueError('; '.join(info.issues))
        if info.price is not None and info.price.output_ceiling is not None:
            if settings.max_output_tokens > info.price.output_ceiling:
                raise ValueError(
                    'Output token cap exceeds the selected model ceiling'
                )
        settings = dataclasses.replace(settings, model_alias=alias)
        spend = ledger.SpendLedger(
            shared / 'runs/spend.sqlite3', ROOT / 'config/budget.yaml'
        )
        guarded = client.GuardedClient(
            models,
            env,
            spend,
            litellm_transport.LiteLLMTransport(),
            policy=client.RetryPolicy(
                attempts=retry.REQUEST_ATTEMPTS,
                retry_window=retry.WINDOW_S,
            ),
        )
        resolved = dataclasses.asdict(info.spec)
        resolved['budget'] = dataclasses.asdict(spend.settings())
        print(
            f'Paid run {alias}/{args.agent}/{settings.mode}: '
            f'{len(cases)} cases; '
            f'target/case ${settings.limits.cost_usd}; '
            f'target total ${len(cases) * settings.limits.cost_usd}. '
            'Retries also consume the shared budget.',
            flush=True,
        )

        def make_client(task: state.Task) -> contracts.ModelClient:
            del task
            return guarded

    configuration = {
        'agent': settings.to_dict(),
        'model': resolved,
        'source_hash': source_fingerprint(ROOT),
        'memory': settings.memory,
        'memory_frozen': settings.memory_frozen,
        'memory_snapshot_hash': snapshot.digest if snapshot else None,
        'mode': release_arm if is_reference else args.agent,
        **(
            {'reference_policy': reference_agent.POLICY} if is_reference else {}
        ),
        'retry_policy': retry.POLICY,
        'summary_policy': single_call.POLICY
        if args.agent == 'single_call'
        else None,
        'temperature': 0,
        'final': args.final,
        'split_hash': runner.digest(partition.to_json()),
    }
    commit = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True
    ).strip()
    manifest = runner.freeze(
        cases,
        configuration,
        commit=commit,
        dry_run=args.dry_run,
        repeat_id=args.repeat_id,
    )
    manifest['partition'] = partition.to_json()
    manifest['heldout_ids'] = sorted(set(ids) & set(partition.heldout))
    executor: runner.Execute
    if is_reference:
        executor = reference_agent.Executor(make_client, settings, spend)
    elif args.agent == 'single_call':
        executor = single_call.Executor(
            make_client, settings, final=use_reserve, spend=spend
        )
    else:
        executor = agent_adapter.Executor(
            make_client,
            settings,
            final=use_reserve,
            spend=spend,
            memory_store=snapshot.memory if snapshot else None,
            snapshot_owner=snapshot,
        )
    return Prepared(
        args,
        tuple(cases),
        manifest,
        output,
        baseline_root,
        tasks_root,
        answers_path,
        executor,
        spend,
        snapshot,
        'final' if use_reserve else None,
    )


def execute(
    prepared: Prepared, *, compare_reference: bool = True
) -> Evaluation:
    """Execute a prepared run using the common cache and official scorer."""
    args = prepared.options
    charges: list[Decimal] = []
    charge_lock = Lock()
    spend = prepared.spend
    reserve = prepared.reserve

    def execute_attempt(
        case: runner.Case, attempts: Path, run_id: str
    ) -> dict[str, object]:
        if spend is None:
            return prepared.executor(case, attempts, run_id)
        # The runner holds the case cache claim during this callback.
        identity = contracts.Run(run_id, reserve)
        before = spend.counters(identity).spent
        try:
            return prepared.executor(case, attempts, run_id)
        finally:
            charge = spend.counters(identity).spent - before
            with charge_lock:
                charges.append(charge)

    folder, records = runner.run(
        prepared.cases,
        prepared.manifest,
        output_root=prepared.output,
        workers=args.workers,
        resume=args.resume,
        execute=execute_attempt,
    )
    incurred = float(sum(charges, Decimal(0)))
    usage = reporting.summarize_usage(records)
    runner.atomic_json(folder / 'trajectory-summary.json', usage)
    runner.atomic_json(
        folder / 'invocation.json',
        {
            'cost_usd': incurred,
            'memory_source': str(args.memory_path.resolve())
            if args.memory_path
            else None,
        },
    )
    runner.atomic_json(
        folder / 'reports.json',
        {str(record['case_id']): record.get('report') for record in records},
    )
    scored = None
    if args.cases == 'field':
        summary = reporting.field_report(
            folder, records, args.field_root / 'gold'
        )
    else:
        scored = reporting.benchmark_report(
            folder,
            records,
            answers_path=prepared.answers_path,
            tasks_dir=prepared.tasks_root,
        )
        if compare_reference:
            baseline_reference.compare(
                folder,
                scored,
                reference=prepared.baseline_root
                / 'results/phase-02/reference_baseline.json',
                baseline_runs=prepared.baseline_root / 'runs/phase-02',
                split_path=args.split,
                answers_path=prepared.answers_path,
                tasks_dir=prepared.tasks_root,
            )
        summary = dict(scored.summary)
    return Evaluation(
        folder, prepared.manifest, records, summary, scored, usage, incurred
    )
