"""Cache claims, frozen manifests, retry accounting and crash recovery."""

import copy
import dataclasses
from concurrent import futures
from pathlib import Path

import pytest

from incidents_agent.eval import runner
from incidents_agent.llm import contracts


@pytest.fixture
def case(tmp_path):
    """A blind input with no answer metadata."""
    folder = tmp_path / 'case'
    folder.mkdir()
    (folder / 'metrics.csv').write_text('time,cpu\n1,2\n')
    return runner.Case('toy', folder)


def manifest(case: runner.Case, dry: bool = True) -> dict[str, object]:
    """Freeze one synthetic configuration."""
    return runner.freeze([case], {'model': 'fake'}, commit='abc', dry_run=dry)


def execute(
    case: runner.Case, attempts: Path, run_id: str
) -> dict[str, object]:
    """Return one no-cost result while recording actual executions."""
    del case
    runner.atomic_json(attempts / run_id / 'called.json', {'called': True})
    return {'ranks': ['checkout']}


def test_cached_case_is_executed_once_and_resume_skips(tmp_path, case):
    """Resume reads the completed cache without invoking the executor."""
    frozen = manifest(case)
    folder, first = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=execute
    )

    def fail(*args):
        raise AssertionError('cached execution repeated')

    _, second = runner.run(
        [case],
        frozen,
        output_root=tmp_path / 'out',
        execute=fail,
        resume=folder,
    )

    assert second == first
    assert len(list((tmp_path / 'out').rglob('called.json'))) == 1


def test_parallel_scripts_claim_each_case_once(tmp_path, case):
    """Independent runner invocations share the OS lock and completed cache."""
    frozen = manifest(case)

    def invoke(_):
        return runner.run(
            [case], frozen, output_root=tmp_path / 'out', execute=execute
        )

    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(invoke, range(2)))

    assert outputs[0][1] == outputs[1][1]
    assert len(list((tmp_path / 'out').rglob('called.json'))) == 1


def test_retry_exhaustion_keeps_case_as_miss(tmp_path, case):
    """Two ordinary failures produce one empty ranking, not a dropped case."""

    def fail(case, attempts, run_id):
        runner.atomic_json(attempts / run_id / 'usage.json', {'cost_usd': 0.01})
        raise ValueError('synthetic failure')

    _, records = runner.run(
        [case], manifest(case), output_root=tmp_path / 'out', execute=fail
    )

    assert records[0]['ranks'] == []
    assert isinstance(records[0]['attempts'], list)
    assert len(records[0]['attempts']) == 2
    assert isinstance(records[0]['usage'], dict)
    assert records[0]['usage']['cost_usd'] == 0.02


@pytest.mark.parametrize(
    'exception', [contracts.BillingUncertain, contracts.BudgetRefused]
)
def test_uncertain_billing_and_budget_refusal_are_not_retried(
    tmp_path, case, exception
):
    """A refused/uncertain attempt cannot be replayed by case-level retries."""

    def fail(*args):
        raise exception('do not retry')

    _, records = runner.run(
        [case], manifest(case), output_root=tmp_path / 'out', execute=fail
    )

    assert isinstance(records[0]['attempts'], list)
    assert len(records[0]['attempts']) == 1
    assert records[0]['ranks'] == []


def test_dry_and_real_caches_are_disjoint(tmp_path, case):
    """Fake output never enters the real cache with matching settings."""
    runner.run(
        [case], manifest(case), output_root=tmp_path / 'out', execute=execute
    )
    runner.run(
        [case],
        manifest(case, False),
        output_root=tmp_path / 'out',
        execute=execute,
    )

    assert len(list((tmp_path / 'out').rglob('called.json'))) == 2


def test_resume_configuration_change_rejected(tmp_path, case):
    """The saved run cannot silently adopt modified model settings."""
    frozen = manifest(case)
    folder, _ = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=execute
    )
    changed = runner.freeze(
        [case], {'model': 'other'}, commit='abc', dry_run=True
    )

    with pytest.raises(ValueError, match='Resume configuration'):
        runner.run(
            [case],
            changed,
            output_root=tmp_path / 'out',
            execute=execute,
            resume=folder,
        )


def test_inputs_modified_after_freeze_rejected(tmp_path, case):
    """Inputs cannot change between fingerprinting and execution."""
    frozen = manifest(case)
    (case.folder / 'metrics.csv').write_text('changed')

    with pytest.raises(ValueError, match='inputs changed'):
        runner.run(
            [case], frozen, output_root=tmp_path / 'out', execute=execute
        )


def test_cache_key_ignores_commit_but_changes_for_repeat_and_inputs(case):
    """Documentation commits reuse output; repeats and input changes do not."""
    frozen = manifest(case)
    changed = copy.deepcopy(frozen)
    changed['git_commit'] = 'new'
    assert isinstance(frozen['cases'], list)
    assert isinstance(changed['cases'], list)
    original = runner.cache_key(frozen, frozen['cases'][0])

    assert original == runner.cache_key(changed, changed['cases'][0])
    changed['repeat_id'] = 'repeat-2'
    assert original != runner.cache_key(changed, changed['cases'][0])
    changed = copy.deepcopy(frozen)
    assert isinstance(changed['cases'], list)
    changed['cases'][0]['files']['metrics.csv'] = 'new'
    assert original != runner.cache_key(changed, changed['cases'][0])


def test_interrupted_paid_attempt_is_not_replayed(tmp_path, case):
    """A crash without a durable report remains blocked for reconciliation."""
    frozen = manifest(case, False)
    assert isinstance(frozen['cases'], list)
    key = runner.cache_key(frozen, frozen['cases'][0])
    cache = tmp_path / 'out' / 'real' / 'cache' / key
    runner.atomic_json(
        cache / 'attempts.json',
        {'attempts': [{'run_id': 'old', 'status': 'running'}]},
    )

    def fail(*args):
        raise AssertionError('interrupted paid attempt replayed')

    _, records = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=fail
    )

    assert records[0]['error'] == 'interrupted_paid_attempt'
    assert records[0]['ranks'] == []


def test_durable_attempt_result_recovers_after_crash(tmp_path, case):
    """A crash between attempt completion and cache publication never repays."""
    frozen = manifest(case, False)
    assert isinstance(frozen['cases'], list)
    key = runner.cache_key(frozen, frozen['cases'][0])
    cache = tmp_path / 'out' / 'real' / 'cache' / key
    runner.atomic_json(
        cache / 'attempts.json',
        {'attempts': [{'run_id': 'old', 'status': 'running'}]},
    )
    runner.atomic_json(
        cache / 'attempts/old/execution.json', {'ranks': ['checkout']}
    )

    def fail(*args):
        raise AssertionError('completed attempt replayed')

    _, records = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=fail
    )

    assert records[0]['ranks'] == ['checkout']


def test_manifest_cannot_swap_execution_case(tmp_path, case):
    """Execution uses exactly the frozen task folders and suite."""
    with pytest.raises(ValueError, match='do not match'):
        runner.run(
            [dataclasses.replace(case, case_id='other')],
            manifest(case),
            output_root=tmp_path / 'out',
            execute=execute,
        )
