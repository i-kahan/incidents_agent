"""Inspect the shared spend ledger and projected evaluation allocations."""

import argparse
from decimal import Decimal
from pathlib import Path

from incidents_agent.llm import config
from incidents_agent.llm import ledger

BUDGET_FILE = Path('config/budget.yaml')
ALLOCATION_FILE = Path('config/budget-allocation.yaml')


def open_ledger(
    override: str | None, env: dict[str, str]
) -> ledger.SpendLedger:
    """Open the project ledger using one invocation's fixed override."""
    return ledger.SpendLedger(
        ledger.DEFAULT_PATH,
        BUDGET_FILE,
        override=override,
        env_override=env.get(config.BUDGET_ENV),
    )


def _print_allocations(limit: Decimal) -> None:
    weights: dict[str, int | None] = {}
    for label, weight in config.read_mapping(ALLOCATION_FILE).items():
        if weight is not None and (type(weight) is not int or weight <= 0):
            raise config.ConfigError(f'Invalid allocation weight for {label}')
        weights[label] = weight
    total = sum(weight for weight in weights.values() if weight is not None)
    if not total:
        raise config.ConfigError('Allocation weights must contain funded rows')
    print('\nPriority  Use                                     Planned USD')
    for priority, (label, weight) in enumerate(weights.items(), 1):
        amount = (
            'from savings'
            if weight is None
            else f'{limit * weight / total:.2f}'
        )
        print(f'{priority:>8}  {label:<39} {amount}')


def main() -> int:
    """Print the current budget and optionally reconcile a billed attempt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget-usd')
    parser.add_argument('--reconcile', metavar='ATTEMPT_ID')
    parser.add_argument('--actual-usd')
    parser.add_argument('--evidence', metavar='BILLING_REFERENCE')
    args = parser.parse_args()
    reconciliation = (args.reconcile, args.actual_usd, args.evidence)
    if any(value is not None for value in reconciliation) and not all(
        value is not None for value in reconciliation
    ):
        parser.error('reconciliation requires all three reconciliation options')
    try:
        spend = open_ledger(args.budget_usd, config.environment(Path('.env')))
        if args.reconcile is not None:
            spend.reconcile(
                args.reconcile,
                config.money(args.actual_usd, 'actual_usd'),
                evidence=args.evidence,
            )
        snapshot = spend.snapshot()
        print(f'Limit:     ${snapshot.limit:f}')
        print(f'Spent:     ${snapshot.spent:f}')
        print(f'Held:      ${snapshot.held:f}')
        print(f'Remaining: ${snapshot.remaining:f}')
        for name, left in snapshot.reserves_left.items():
            print(f'Reserve {name}: ${left:f} remaining')
        if snapshot.halted:
            print('Admissions halted pending billing reconciliation.')
        _print_allocations(snapshot.limit)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
