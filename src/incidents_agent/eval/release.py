"""Register and resume the single Phase 10 evaluation in the shared cache."""

import contextlib
import dataclasses
import fcntl
import hashlib
import subprocess
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

from incidents_agent.eval import failures
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import split

REQUIRED = ('primary', 'single-call', 'field')
OPTIONAL = ('memory', 'multi', 'rcaeval-reference')
ARMS = REQUIRED + OPTIONAL


def hash_file(path: Path) -> str:
    """Hash a scoring input or piece of release evidence."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def claim(root: Path) -> Iterator[None]:
    """Serialize releases across processes and worktrees."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'claim.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prepare(arm: str, shared: Path) -> service.Prepared:
    """Resolve one named arm with no caller overrides to model or split."""
    options = service.Options(
        cases='field' if arm == 'field' else 'heldout',
        workers=4,
        answers=shared / 'agent_tasks/heldout.answers.jsonl',
        model='gpt-small',
        agent='single_call' if arm == 'single-call' else 'single',
        mode='multi' if arm == 'multi' else 'single',
        final=arm != 'field',
        repeat_id=f'phase-10-{arm}',
        memory=arm == 'memory',
        memory_path=shared / 'runs/phase-09/memory-seed'
        if arm == 'memory'
        else None,
    )
    prepared = service.prepare(options, release_arm=arm)
    if arm != 'field':
        partition = split.from_json(runner.read_object(options.split))
        ids = {case.case_id for case in prepared.cases}
        if len(prepared.cases) != 60 or ids != set(partition.heldout):
            raise ValueError('Phase 10 requires all 60 locked held-out cases')
    configuration = prepared.manifest['configuration']
    assert isinstance(configuration, dict)
    if configuration['memory'] != (arm == 'memory'):
        raise ValueError('Unexpected memory setting for this arm')
    return prepared


def scoring_inputs(shared: Path) -> dict[str, str]:
    """Hash held-out answers, field gold and baseline predictions."""
    result = {}
    names = ('baro', 'guarded_baro', 'mmbaro', 'mmbaro_candidates')
    for name in names:
        relative = f'runs/phase-02/{name}.heldout.predictions.jsonl'
        if name in names[:2] or (shared / relative).is_file():
            result[relative] = hash_file(shared / relative)
    result['agent_tasks/heldout.answers.jsonl'] = hash_file(
        shared / 'agent_tasks/heldout.answers.jsonl'
    )
    for relative, value in runner.file_hashes(
        service.ROOT / 'evalsets/field/gold'
    ).items():
        result[f'evalsets/field/gold/{relative}'] = value
    return result


def _gate(
    path: Path, primary: service.Prepared
) -> tuple[dict[str, dict[str, str]], Decimal, list[str]]:
    evidence = runner.read_object(path)
    files = {}
    for key in ('tuning', 'audit', 'browser', 'prerequisites'):
        source = evidence.get(key)
        if not isinstance(source, str) or not source:
            raise ValueError(f'Release evidence must identify {key}')
        target = path.parent / source
        files[key] = {'file': target.name, 'sha256': hash_file(target)}
    try:
        estimate = Decimal(str(evidence.get('final_estimate_usd')))
    except ArithmeticError as exc:
        raise ValueError('Invalid final estimate') from exc
    if not estimate.is_finite() or estimate <= 0:
        raise ValueError('Final estimate must be finite and positive')
    audit = runner.read_object(path.parent / str(evidence['audit']))
    actions = audit.get('actions')
    if not isinstance(actions, dict) or not actions:
        raise ValueError('Missing human action audit')
    if audit.get('sample_size') != 15 or any(
        not isinstance(row, dict) or row.get('total') != row.get('completed')
        for row in (audit.get('support'), *actions.values())
    ):
        raise ValueError('Human audit judgments are incomplete')
    tuning = path.parent / str(evidence['tuning'])
    saved = runner.read_object(tuning).get('frozen')
    current = primary.manifest['configuration']
    assert isinstance(current, dict)
    partition = split.from_json(runner.read_object(primary.options.split))
    if not isinstance(saved, dict) or not isinstance(
        saved.get('configuration'), dict
    ):
        raise ValueError('Missing tuning run manifest')
    if saved.get('dry_run') is not False or any(
        saved['configuration'].get(key) != current[key]
        for key in (
            'agent',
            'model',
            'source_hash',
            'split_hash',
            'retry_policy',
        )
    ):
        raise ValueError('Selected tuning run does not match the final agent')
    results = tuning.parent / 'results.json'
    rows = failures.indexed(results)
    if set(rows) != set(partition.tuning):
        raise ValueError('Tuning results must cover the locked tuning set')
    served = sorted(
        {
            model
            for row in rows.values()
            for attempt in (
                row['attempts'] if isinstance(row['attempts'], list) else []
            )
            for model in attempt.get('served_models', [])
            if model
        }
    )
    if not served:
        raise ValueError('Tuning results must identify the served model')
    files['tuning_results'] = {
        'file': results.name,
        'sha256': hash_file(results),
    }
    files['receipt'] = {'file': path.name, 'sha256': hash_file(path)}
    return files, estimate, served


def freeze(root: Path, shared: Path, evidence: Path) -> None:
    """Record frozen inputs and evidence before paid execution."""
    with claim(root):
        target = root / 'release.json'
        if target.exists():
            raise ValueError('Phase 10 is already frozen')
        head = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=service.ROOT, text=True
        ).strip()
        upstream = subprocess.check_output(
            ['git', 'rev-parse', 'origin/main'], cwd=service.ROOT, text=True
        ).strip()
        if (
            head != upstream
            or subprocess.check_output(
                ['git', 'status', '--porcelain', '--untracked-files=no'],
                cwd=service.ROOT,
                text=True,
            ).strip()
        ):
            raise ValueError('Freeze requires a clean checkout at origin/main')
        arms = {
            name: prepare(name, shared)
            for name in (*REQUIRED, 'multi', 'rcaeval-reference')
        }
        evidence_hashes, estimate, served = _gate(evidence, arms['primary'])
        spend = arms['primary'].spend
        assert spend is not None
        budget = spend.snapshot()
        if (
            budget.halted
            or budget.held
            or estimate > budget.reserves_left.get('final', Decimal(0))
            or estimate > budget.remaining
        ):
            raise ValueError('Reconcile spend and confirm the final reserve')
        manifests = {name: arm.manifest for name, arm in arms.items()}
        manifests['memory'] = (
            prepare('memory', shared).manifest
            if (shared / 'runs/phase-09/memory-seed').exists()
            else {}
        )
        runner.atomic_json(
            target,
            {
                'version': 1,
                'commit': head,
                'dependency_lock_sha256': hash_file(service.ROOT / 'uv.lock'),
                'expected_served_models': served,
                'evidence': evidence_hashes,
                'budget': dataclasses.asdict(budget),
                'final_estimate_usd': estimate,
                'scoring_inputs': scoring_inputs(shared),
                'arms': manifests,
            },
        )


def run_folder(prepared: service.Prepared, arm: str) -> Path:
    """Return the sole cache run directory assigned to an arm."""
    return prepared.output / 'real' / 'runs' / f'phase-10-{arm}'


def run_arm(root: Path, shared: Path, arm: str) -> Path:
    """Resume a registered arm, checkpointing its identity before any calls."""
    with claim(root):
        frozen = runner.read_object(root / 'release.json')
        manifests = frozen.get('arms')
        if not isinstance(manifests, dict) or not manifests.get(arm):
            raise ValueError(f'No guarded, frozen executor for {arm}')
        completed = root / f'{arm}.complete.json'
        if (root / f'{arm}.omitted.json').exists():
            raise ValueError(f'{arm} was omitted from this release')
        for previous in REQUIRED:
            if previous == arm:
                break
            if not (root / f'{previous}.complete.json').is_file():
                raise ValueError(f'Complete {previous} before {arm}')
        if (
            arm == 'multi'
            and manifests.get('memory')
            and not any(
                (root / name).is_file()
                for name in ('memory.complete.json', 'memory.omitted.json')
            )
        ):
            raise ValueError('Complete or omit the memory arm first')
        if arm == 'rcaeval-reference' and not any(
            (root / name).is_file()
            for name in ('multi.complete.json', 'multi.omitted.json')
        ):
            raise ValueError('Complete or omit the multi arm first')
        prepared = prepare(arm, shared)
        if prepared.manifest != manifests[arm] or (
            scoring_inputs(shared) != frozen['scoring_inputs']
        ):
            raise ValueError('Frozen configuration, commit or inputs changed')
        folder = run_folder(prepared, arm)
        if completed.is_file():
            return folder
        spend = prepared.spend
        assert spend is not None
        if spend.snapshot().halted:
            raise ValueError('Reconcile spend before running a paid arm')
        manifest = folder / 'manifest.json'
        if not manifest.exists():
            runner.atomic_json(manifest, {'frozen': prepared.manifest})
        invocation = service.execute(
            dataclasses.replace(
                prepared,
                options=dataclasses.replace(prepared.options, resume=folder),
            ),
            compare_reference=False,
        )
        runner.atomic_json(
            completed,
            {
                'results_sha256': hash_file(folder / 'results.json'),
                'invocation_cost_usd': invocation.invocation_cost_usd,
                'budget': dataclasses.asdict(spend.snapshot()),
            },
        )
        return folder
