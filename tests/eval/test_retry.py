"""Failure-specific retry decisions and safe attempt recovery."""

import time

import pytest

from incidents_agent.eval import retry
from incidents_agent.eval import runner
from incidents_agent.llm import contracts


@pytest.mark.parametrize(
    'error,held,settled,category,retryable',
    [
        (contracts.RetryDeferred(0), False, False, 'provider_deferred', True),
        (contracts.RetryDeferred(91), False, False, 'provider_deferred', False),
        (
            contracts.RetryDeferred(0),
            True,
            False,
            'billing_reconciliation_required',
            False,
        ),
        (
            contracts.BillingUncertain('x'),
            False,
            False,
            'billing_reconciliation_required',
            False,
        ),
        (contracts.BudgetRefused('x'), False, False, 'budget_refused', False),
        (
            contracts.ModelUnavailable('x'),
            False,
            False,
            'provider_permanent',
            False,
        ),
        (
            contracts.ProviderError('x'),
            False,
            False,
            'provider_permanent',
            False,
        ),
        (ValueError('x'), False, False, 'invalid_input', False),
        (FileNotFoundError('x'), False, False, 'invalid_input', False),
        (ValueError('x'), False, True, 'invalid_response', True),
    ],
)
def test_error_policy(error, held, settled, category, retryable):
    """Billing uncertainty takes precedence over otherwise retryable errors."""
    failure = retry.classify(error, held=held, settled=settled)

    assert failure.category == category
    assert failure.retryable is retryable


def test_programming_errors_are_not_classified_as_misses():
    """Unexpected exceptions must stop the run for repair."""
    with pytest.raises(RuntimeError, match='bug'):
        retry.classify(RuntimeError('bug'))


def test_primary_only_strategy_has_no_implicit_fallback():
    """A second failure cannot switch models or create a third attempt."""
    strategy = retry.PrimaryOnly()
    failure = retry.classify(contracts.RetryDeferred(0))

    assert strategy.next_model('haiku', failure, [{}]) == 'haiku'
    assert strategy.next_model('haiku', failure, [{}, {}]) is None
    assert (
        strategy.next_model(
            'haiku', retry.classify(contracts.ProviderError('x')), [{}]
        )
        is None
    )


@pytest.fixture
def frozen_case(tmp_path):
    """Freeze a minimal case for executor-level policy tests."""
    folder = tmp_path / 'case'
    folder.mkdir()
    (folder / 'data.csv').write_text('a\n1\n')
    case = runner.Case('x', folder)
    return case, runner.freeze([case], {}, commit='test', dry_run=True)


@pytest.mark.parametrize(
    'error', [contracts.ProviderError('x'), contracts.ModelUnavailable('x')]
)
def test_permanent_provider_error_is_not_retried(tmp_path, frozen_case, error):
    """Authentication/model/request failures produce one terminal miss."""
    case, frozen = frozen_case

    def execute(*args):
        raise error

    _, records = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=execute
    )

    assert isinstance(records[0]['attempts'], list)
    assert len(records[0]['attempts']) == 1
    assert records[0]['stop_reason'] == 'provider_permanent'
    assert records[0]['ranks'] == []


def test_retry_after_honored_once(tmp_path, frozen_case, monkeypatch):
    """The runner owns a single retry and honors its provider delay."""
    case, frozen = frozen_case
    sleeps: list[float] = []
    monkeypatch.setattr(time, 'sleep', sleeps.append)
    monkeypatch.setattr(time, 'time', lambda: 100.0)

    def execute(*args):
        raise contracts.RetryDeferred(2)

    _, records = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=execute
    )

    assert sleeps == [2.0]
    assert isinstance(records[0]['attempts'], list)
    assert len(records[0]['attempts']) == 2
    assert records[0]['error'] == 'RetryDeferred'
    assert records[0]['retry_exhausted'] is True


def test_billing_hold_prevents_transient_retry(tmp_path, frozen_case):
    """An ambiguous network failure cannot consume another paid attempt."""
    case, frozen = frozen_case

    def execute(case, attempts, run_id):
        runner.atomic_json(attempts / run_id / 'usage.json', {'held_usd': 0.02})
        raise contracts.RetryDeferred(0)

    _, records = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=execute
    )

    assert isinstance(records[0]['attempts'], list)
    assert len(records[0]['attempts']) == 1
    assert records[0]['stop_reason'] == 'billing_reconciliation_required'


def test_persistence_failure_aborts_without_retry(tmp_path, frozen_case):
    """Disk failures cannot be mistaken for retryable provider failures."""
    case, frozen = frozen_case

    def execute(*args):
        raise OSError('disk full')

    with pytest.raises(OSError, match='disk full'):
        runner.run(
            [case], frozen, output_root=tmp_path / 'out', execute=execute
        )


def test_recover_durable_response_marker(tmp_path, frozen_case):
    """The runner invokes recovery when only a settled response is durable."""
    case, frozen = frozen_case
    frozen['dry_run'] = False
    key = runner.cache_key(frozen, frozen['cases'][0])
    cache = tmp_path / 'out/real/cache' / key
    runner.atomic_json(
        cache / 'attempts.json',
        {'attempts': [{'run_id': 'old', 'status': 'running'}]},
    )
    runner.atomic_json(
        cache / 'attempts/old/response.json', {'message': 'settled'}
    )

    def recover(case, attempts, run_id):
        assert run_id == 'old'
        return {'ranks': ['checkout']}

    _, records = runner.run(
        [case], frozen, output_root=tmp_path / 'out', execute=recover
    )

    assert records[0]['ranks'] == ['checkout']
    assert isinstance(records[0]['attempts'], list)
    assert len(records[0]['attempts']) == 1
