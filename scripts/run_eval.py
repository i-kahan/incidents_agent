"""Run one frozen, cached evaluation configuration through the Phase 5 agent."""

import argparse
import dataclasses
import json
import subprocess
from pathlib import Path

from incidents_agent.agent import config as agent_config
from incidents_agent.agent import fake_policy
from incidents_agent.agent import state
from incidents_agent.eval import agent_adapter
from incidents_agent.eval import baseline_reference
from incidents_agent.eval import field
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.eval import selection
from incidents_agent.eval import split
from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger
from incidents_agent.llm import litellm_transport
from incidents_agent.llm import pricing

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint(root: Path) -> str:
    """Hash output-affecting code and locked dependencies, not report prose."""
    inputs = {}
    for part in ('agent', 'tools', 'ingest', 'llm'):
        for path in sorted(
            (root / 'src' / 'incidents_agent' / part).glob('*.py')
        ):
            inputs[str(path.relative_to(root))] = path.read_text()
    for relative in ('uv.lock', 'src/incidents_agent/eval/agent_adapter.py'):
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


def main() -> int:
    """Select cases, freeze configuration, execute and score separately."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', default='tuning')
    parser.add_argument('--model')
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
    args = parser.parse_args()
    shared = shared_root()
    baseline_root = args.baseline_root or shared
    tasks_root = args.tasks_root or shared / 'agent_tasks/re2-ob'
    output = args.output_root or shared / 'runs/phase-06'
    partition = split.from_json(json.loads(args.split.read_text()))
    try:
        ids = selection.resolve(
            args.cases,
            partition,
            final=args.final,
            limit=args.limit,
            field_ids=field.CASE_IDS,
        )
        settings = agent_config.load_agent_config(args.config)
        if args.model:
            settings = dataclasses.replace(settings, model_alias=args.model)
        suite = 'field' if args.cases == 'field' else 'rcaeval'
        base = args.field_root if suite == 'field' else tasks_root
        cases = [
            runner.Case(case_id, base / 'cases' / case_id, suite)
            for case_id in ids
        ]
        spend: ledger.SpendLedger | None = None
        resolved: dict[str, object] = {'model': 'rule-fake'}
        if args.dry_run:

            def make_client(task: state.Task) -> contracts.ModelClient:
                return fake_policy.RuleClient(task.candidate_services)
        else:
            if args.split.resolve() != (ROOT / 'splits/re2-ob.json').resolve():
                raise ValueError(
                    'Paid runs require the repository locked split'
                )
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
            info = client.model_info(
                models, env, pricing.bundled_catalog(), alias
            )
            if not info.usable:
                raise ValueError('; '.join(info.issues))
            settings = dataclasses.replace(settings, model_alias=alias)
            spend = ledger.SpendLedger(
                shared / 'runs/spend.sqlite3', ROOT / 'config/budget.yaml'
            )
            guarded = client.GuardedClient(
                models, env, spend, litellm_transport.LiteLLMTransport()
            )
            resolved = dataclasses.asdict(info.spec)
            resolved['budget'] = dataclasses.asdict(spend.settings())
            print(
                f'Paid run: {len(cases)} cases; '
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
            'memory': False,
            'mode': 'single',
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
        folder, records = runner.run(
            cases,
            manifest,
            output_root=output,
            workers=args.workers,
            resume=args.resume,
            execute=agent_adapter.Executor(
                make_client, settings, final=args.final, spend=spend
            ),
        )
        runner.atomic_json(
            folder / 'trajectory-summary.json',
            reporting.summarize_usage(records),
        )
        runner.atomic_json(
            folder / 'reports.json',
            {str(r['case_id']): r.get('report') for r in records},
        )
        if suite == 'field':
            summary = reporting.field_report(
                folder, records, args.field_root / 'gold'
            )
        else:
            answers_path = args.answers or shared / 'agent_tasks' / (
                'tuning.answers.jsonl'
                if set(ids) <= set(partition.tuning)
                else 're2-ob.answers.jsonl'
            )
            scored = reporting.benchmark_report(
                folder, records, answers_path=answers_path, tasks_dir=tasks_root
            )
            baseline_reference.compare(
                folder,
                scored,
                reference=args.baseline_root
                / 'results/phase-02/reference_baseline.json',
                baseline_runs=baseline_root / 'runs/phase-02',
                split_path=args.split,
                answers_path=answers_path,
                tasks_dir=tasks_root,
            )
            summary = dict(scored.summary)
        print(
            json.dumps(
                {
                    'output': str(folder),
                    'dry_run': args.dry_run,
                    'summary': summary,
                },
                indent=2,
            )
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
