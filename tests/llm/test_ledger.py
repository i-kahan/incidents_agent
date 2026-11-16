"""Behavior of the persisted admission and settlement rules."""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from incidents_agent.llm import contracts
from incidents_agent.llm import ledger


def _reserve(
    spend: ledger.SpendLedger,
    amount: str,
    *,
    run: str = 'run',
    reserve: str | None = None,
    case: str | None = None,
) -> ledger.Reservation:
    return spend.reserve(
        contracts.Run(run, reserve), case, 'fake', Decimal(amount)
    )


_WORKER = """
import sys
from decimal import Decimal
from pathlib import Path
from incidents_agent.llm import contracts, ledger

spend = ledger.SpendLedger(Path(sys.argv[1]), Path(sys.argv[2]))
print('ready', flush=True)
input()
try:
    spend.reserve(contracts.Run(sys.argv[3]), None, 'fake', Decimal('1'))
except contracts.BudgetRefused:
    print('refused')
else:
    print('admitted')
"""


@pytest.mark.integration
def test_independent_processes_cannot_spend_the_same_balance(
    spend: ledger.SpendLedger, budget_file: Path
) -> None:
    """Race two workers for a balance that covers one call."""
    command = [sys.executable, '-c', _WORKER, str(spend.path), str(budget_file)]
    with (
        subprocess.Popen(
            [*command, 'one'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as first,
        subprocess.Popen(
            [*command, 'two'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as second,
    ):
        assert first.stdout and second.stdout and first.stdin and second.stdin
        assert first.stdout.readline().strip() == 'ready'
        assert second.stdout.readline().strip() == 'ready'

        first.stdin.write('start\n')
        second.stdin.write('start\n')
        first.stdin.flush()
        second.stdin.flush()
        admitted = [
            first.communicate(timeout=10)[0].strip(),
            second.communicate(timeout=10)[0].strip(),
        ]

    assert sorted(admitted) == ['admitted', 'refused']
    assert spend.snapshot().held == Decimal('1')


def test_settling_releases_the_unused_reservation(
    spend: ledger.SpendLedger,
) -> None:
    """Return the difference between a bound and an actual charge."""
    hold = _reserve(spend, '1.25')

    spend.finish(hold, 'success', cost=Decimal('0.25'))

    status = spend.snapshot()
    assert (status.spent, status.held, status.remaining) == (
        Decimal('0.25'),
        Decimal(0),
        Decimal('1.50'),
    )


def test_only_named_run_can_use_protected_balance(
    spend: ledger.SpendLedger,
) -> None:
    """Keep general spending out of the final reserve."""
    _reserve(spend, '1.25')

    with pytest.raises(contracts.BudgetRefused, match='named reserves'):
        _reserve(spend, '0.01', run='another')

    assert _reserve(spend, '0.50', run='final-run', reserve='final').bound == (
        Decimal('0.50')
    )
    with pytest.raises(contracts.BudgetRefused, match='Global'):
        _reserve(spend, '0.01', run='final-run', reserve='final')


def test_named_run_cannot_use_more_than_its_reserve(
    spend: ledger.SpendLedger,
) -> None:
    """Enforce the selected reserve amount."""
    with pytest.raises(contracts.BudgetRefused, match='exhausted'):
        _reserve(spend, '0.51', reserve='final')


def test_reserve_binding_is_fixed_for_a_run(spend: ledger.SpendLedger) -> None:
    """Do not let a run switch pools after starting."""
    _reserve(spend, '0.01')
    with pytest.raises(contracts.BudgetRefused, match='cannot be changed'):
        _reserve(spend, '0.01', reserve='final')


def test_per_case_usage_is_shared_by_workers_of_the_same_run(
    spend: ledger.SpendLedger, budget_file: Path
) -> None:
    """Aggregate a case across workers and isolate independent runs."""
    hold = _reserve(spend, '0.50', case='case')
    spend.finish(hold, 'success', cost=Decimal('0.40'))
    worker = ledger.SpendLedger(spend.path, budget_file)

    with pytest.raises(contracts.BudgetRefused, match='Per-case'):
        _reserve(worker, '0.36', case='case')
    assert _reserve(worker, '0.36', run='comparison', case='case').bound == (
        Decimal('0.36')
    )


def test_limit_changes_reuse_existing_spend(
    spend: ledger.SpendLedger, budget_file: Path
) -> None:
    """Apply file changes to the next admission without resetting spend."""
    hold = _reserve(spend, '1')
    spend.finish(hold, 'success', cost=Decimal('1'))
    budget_file.write_text('total_usd: 0.9\nsafety_margin_usd: 0\n')

    with pytest.raises(contracts.BudgetRefused, match='Global'):
        _reserve(spend, '0')

    budget_file.write_text('total_usd: 2\nsafety_margin_usd: 0\n')
    _reserve(spend, '1')
    assert spend.snapshot().remaining == 0


@pytest.mark.parametrize('status', ['reserved', 'ambiguous'])
def test_reconciliation_releases_only_unused_held_amount(
    spend: ledger.SpendLedger, status: str
) -> None:
    """Retain recorded spend after resolving an uncertain charge."""
    hold = _reserve(spend, '1')
    if status == 'ambiguous':
        spend.finish(hold, 'ambiguous', reason='timeout')
    assert spend.snapshot().held == Decimal('1')

    spend.reconcile(hold.id, Decimal('0.2'), evidence='invoice-row-123')

    assert spend.snapshot().held == 0
    assert spend.snapshot().spent == Decimal('0.2')
    with pytest.raises(ValueError, match='No unresolved attempt'):
        spend.reconcile(hold.id, Decimal(0), evidence='invoice')


def test_overrun_halts_admissions(spend: ledger.SpendLedger) -> None:
    """Require reconciliation before admitting calls after an overrun."""
    hold = _reserve(spend, '0.25')
    assert spend.finish(hold, 'success', cost=Decimal('0.30'))

    with pytest.raises(contracts.BudgetRefused, match='overrun'):
        _reserve(spend, '0.01')

    spend.reconcile(hold.id, Decimal('0.29'), evidence='invoice')
    assert _reserve(spend, '0.01').bound == Decimal('0.01')
