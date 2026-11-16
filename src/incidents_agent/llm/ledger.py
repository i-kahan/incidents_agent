"""Persistent spend reservations and run accounting shared by all workers."""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from decimal import localcontext
from pathlib import Path
from typing import Literal

from incidents_agent.llm import config
from incidents_agent.llm import contracts

DEFAULT_PATH = Path('runs/spend.sqlite3')
_ZERO = Decimal(0)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    reserve_name TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    case_id TEXT,
    requested_model TEXT NOT NULL,
    started_at TEXT NOT NULL,
    limit_usd TEXT NOT NULL,
    bound_usd TEXT NOT NULL,
    status TEXT NOT NULL,
    cost_usd TEXT,
    served_model TEXT,
    request_id TEXT,
    usage TEXT,
    reason TEXT,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS attempts_run_id ON attempts(run_id);
"""

type Status = Literal[
    'reserved', 'success', 'unbilled', 'ambiguous', 'overrun', 'reconciled'
]


@dataclass(frozen=True, slots=True)
class Reservation:
    """An admitted attempt and the limit used to admit it."""

    id: str
    limit: Decimal
    bound: Decimal
    started_at: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Amounts committed to calls under the current effective limit."""

    limit: Decimal
    spent: Decimal
    held: Decimal
    reserves_left: dict[str, Decimal]
    halted: bool

    @property
    def remaining(self) -> Decimal:
        """Return the uncommitted balance, including any named reserve."""
        with localcontext(prec=48):
            return self.limit - self.spent - self.held


@dataclass(frozen=True, slots=True)
class RunCounters:
    """Usage and cost summed across a run's workers."""

    attempts: int
    calls: int
    usage: contracts.Usage
    spent: Decimal
    held: Decimal


class SpendLedger:
    """Read and update one spend ledger using SQLite write transactions."""

    def __init__(
        self,
        path: Path,
        budget_path: Path,
        *,
        override: str | None = None,
        env_override: str | None = None,
    ) -> None:
        """Open the ledger and validate the invocation settings."""
        self.path = path.resolve()
        self._budget_path = budget_path.resolve()
        self._override = override if override is not None else env_override
        self.settings()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def settings(self) -> config.BudgetSettings:
        """Return the current file settings for this invocation."""
        return config.load_budget(
            self._budget_path, override=self._override, environ={}
        )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA synchronous = FULL')
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with localcontext(prec=48), closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @staticmethod
    def _amounts(row: sqlite3.Row) -> tuple[Decimal, Decimal]:
        cost = _ZERO if row['cost_usd'] is None else Decimal(row['cost_usd'])
        if row['status'] in ('reserved', 'ambiguous'):
            return cost, Decimal(row['bound_usd'])
        return cost, _ZERO

    @classmethod
    def _totals(
        cls,
        rows: list[sqlite3.Row],
        settings: config.BudgetSettings,
    ) -> Snapshot:
        spent = held = _ZERO
        used = dict.fromkeys(settings.reserves, _ZERO)
        for row in rows:
            cost, open_amount = cls._amounts(row)
            spent += cost
            held += open_amount
            if row['reserve_name'] in used:
                used[row['reserve_name']] += cost + open_amount
        return Snapshot(
            settings.limit,
            spent,
            held,
            {
                name: max(_ZERO, amount - used[name])
                for name, amount in settings.reserves.items()
            },
            any(row['status'] == 'overrun' for row in rows),
        )

    @staticmethod
    def _rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
        return db.execute(
            'SELECT attempts.*, runs.reserve_name FROM attempts '
            'JOIN runs ON runs.id = attempts.run_id'
        ).fetchall()

    def snapshot(self) -> Snapshot:
        """Return global amounts for the budget display."""
        with localcontext(prec=48), closing(self._connect()) as db:
            return self._totals(self._rows(db), self.settings())

    def reserve(
        self,
        run: contracts.Run,
        case_id: str | None,
        requested_model: str,
        bound: Decimal,
    ) -> Reservation:
        """Commit the full bound before dispatch or raise BudgetRefused."""
        if not bound.is_finite() or bound < 0:
            raise ValueError('Reservation must be finite and nonnegative')
        with self._write() as db:
            settings = self.settings()
            rows = self._rows(db)
            totals = self._totals(rows, settings)
            reserve = run.reserve_name
            existing = db.execute(
                'SELECT reserve_name FROM runs WHERE id = ?', (run.id,)
            ).fetchone()
            if existing is not None and existing['reserve_name'] != reserve:
                raise contracts.BudgetRefused('Run reserve cannot be changed')
            if totals.halted:
                raise contracts.BudgetRefused(
                    'A reported overrun needs billing reconciliation'
                )
            if bound > totals.remaining:
                raise contracts.BudgetRefused('Global spend limit reached')
            if reserve is not None:
                if reserve not in totals.reserves_left:
                    raise contracts.BudgetRefused(
                        f'Unknown reserve {reserve!r}'
                    )
                if bound > totals.reserves_left[reserve]:
                    raise contracts.BudgetRefused(
                        f'Reserve {reserve!r} exhausted'
                    )
            protected = sum(
                left
                for name, left in totals.reserves_left.items()
                if name != reserve
            )
            if bound > totals.remaining - protected:
                raise contracts.BudgetRefused(
                    'Balance is held for named reserves'
                )
            if case_id is not None and settings.per_case is not None:
                case_used = sum(
                    sum(self._amounts(row))
                    for row in rows
                    if (row['run_id'], row['case_id']) == (run.id, case_id)
                )
                if case_used + bound > settings.per_case:
                    raise contracts.BudgetRefused(
                        'Per-case spend limit reached'
                    )
            reservation = Reservation(
                uuid.uuid4().hex,
                settings.limit,
                bound,
                datetime.now(UTC).isoformat(),
            )
            db.execute(
                'INSERT OR IGNORE INTO runs VALUES (?, ?)', (run.id, reserve)
            )
            db.execute(
                'INSERT INTO attempts (id, run_id, case_id, requested_model, '
                'started_at, limit_usd, bound_usd, status) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved')",
                (
                    reservation.id,
                    run.id,
                    case_id,
                    requested_model,
                    reservation.started_at,
                    str(settings.limit),
                    str(bound),
                ),
            )
        return reservation

    def finish(
        self,
        reservation: Reservation,
        status: Literal['success', 'unbilled', 'ambiguous'],
        *,
        cost: Decimal | None = None,
        response: contracts.ProviderResult | None = None,
        request_id: str | None = None,
        served_model: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Record the outcome and return whether it breached the bound."""
        if status == 'success' and (
            cost is None or not cost.is_finite() or cost < 0
        ):
            raise ValueError('Success requires finite and nonnegative cost')
        if status != 'success' and cost is not None:
            raise ValueError('A failed attempt cannot supply a usage charge')
        with self._write() as db:
            entry = db.execute(
                'SELECT bound_usd FROM attempts '
                "WHERE id=? AND status='reserved'",
                (reservation.id,),
            ).fetchone()
            if entry is None:
                raise ValueError(f'Attempt is not open: {reservation.id}')
            overrun = cost is not None and cost > Decimal(entry['bound_usd'])
            db.execute(
                'UPDATE attempts SET status=?, cost_usd=?, served_model=?, '
                'request_id=?, usage=?, reason=? '
                "WHERE id=? AND status='reserved'",
                (
                    'overrun' if overrun else status,
                    str(cost) if cost is not None else None,
                    response.served_model if response else served_model,
                    response.request_id if response else request_id,
                    json.dumps(asdict(response.usage)) if response else None,
                    reason,
                    reservation.id,
                ),
            )
        return overrun

    def reconcile(
        self, reservation_id: str, actual: Decimal, *, evidence: str
    ) -> None:
        """Settle an inactive attempt using confirmed billing evidence."""
        actual = config.money(actual, 'actual_usd')
        if not evidence.strip():
            raise ValueError('Reconciliation requires a billing reference')
        with self._write() as db:
            updated = db.execute(
                "UPDATE attempts SET status='reconciled', "
                'cost_usd=?, evidence=? '
                "WHERE id=? AND status IN ('reserved', 'ambiguous', 'overrun')",
                (str(actual), evidence, reservation_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f'No unresolved attempt: {reservation_id}')

    def attempt_records(self, run: contracts.Run) -> list[dict[str, object]]:
        """Return JSON-ready metadata for every admitted attempt in a run."""
        with closing(self._connect()) as db:
            rows = db.execute(
                'SELECT * FROM attempts WHERE run_id = ? ORDER BY rowid',
                (run.id,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            record = dict(row)
            if record['usage'] is not None:
                record['usage'] = json.loads(record['usage'])
            result.append(record)
        return result

    def counters(self, run: contracts.Run) -> RunCounters:
        """Return cumulative usage and attempt counts for a run."""
        with closing(self._connect()) as db:
            rows = db.execute(
                'SELECT * FROM attempts WHERE run_id = ?', (run.id,)
            ).fetchall()
        spent = held = _ZERO
        calls = 0
        totals = dict.fromkeys(
            (
                'input_tokens',
                'output_tokens',
                'cache_read_tokens',
                'cache_write_tokens',
                'reasoning_tokens',
            ),
            0,
        )
        with localcontext(prec=48):
            for row in rows:
                cost, open_amount = self._amounts(row)
                spent += cost
                held += open_amount
                if row['usage'] is not None:
                    usage = contracts.Usage(**json.loads(row['usage']))
                    for field in totals:
                        totals[field] += getattr(usage, field)
                    calls += row['status'] in (
                        'success',
                        'overrun',
                        'reconciled',
                    )
        return RunCounters(
            len(rows), calls, contracts.Usage(**totals), spent, held
        )
