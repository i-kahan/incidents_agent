"""The task an agent is given and the budget it spends while working."""

import dataclasses
import json
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal
from typing import Protocol

from incidents_agent.agent import config

type StopReason = Literal[
    'submitted',
    'tool_call_limit',
    'model_turn_limit',
    'cost_cap',
    'wall_time',
    'spend_refused',
    'model_error',
    'reservation_refused',
    'role_wall_time',
]

# Stops that end the run outright. Any other stop still lets verify use the
# capacity reserved for it. `role_wall_time` (one role's time slice in multi
# mode) is not among them: the run goes on with the other roles.
HARD_STOPS: frozenset[str] = frozenset(
    {'wall_time', 'spend_refused', 'model_error', 'reservation_refused'}
)


@dataclasses.dataclass(frozen=True)
class Task:
    """What the agent is asked to investigate.

    Attributes:
        folder: Folder of dropped files.
        case_id: Benchmark case id, used for the per-case spend cap.
        question: Optional free-text description from the user.
        fault_start_s: Fault start time in Unix seconds, when known.
        candidate_services: Closed answer list for benchmark cases.
        candidate_indicators: Closed indicator list for benchmark cases.
    """

    folder: Path
    case_id: str | None = None
    question: str = ''
    fault_start_s: float | None = None
    candidate_services: tuple[str, ...] = ()
    candidate_indicators: tuple[str, ...] = ()

    @property
    def has_candidates(self) -> bool:
        """Return True when ranks must come from a closed list."""
        return bool(self.candidate_services or self.candidate_indicators)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return {
            'folder': str(self.folder),
            'case_id': self.case_id,
            'question': self.question,
            'fault_start_s': self.fault_start_s,
            'candidate_services': list(self.candidate_services),
            'candidate_indicators': list(self.candidate_indicators),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'Task':
        """Rebuild a task written by `to_dict`."""
        start = data.get('fault_start_s')
        case_id = data.get('case_id')
        return cls(
            folder=Path(str(data['folder'])),
            case_id=None if case_id is None else str(case_id),
            question=str(data.get('question') or ''),
            fault_start_s=None if start is None else float(str(start)),
            candidate_services=_strings(data.get('candidate_services')),
            candidate_indicators=_strings(data.get('candidate_indicators')),
        )

    @classmethod
    def from_task_json(cls, path: Path) -> 'Task':
        """Build a task from an RCAEval `task.json` beside its case files.

        Only the blind task view is read: the fault start time and the
        candidate lists. Answers are never in this file.

        Args:
            path: Path to `task.json`; its folder is the case folder.

        Returns:
            The task for that case.
        """
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError(f'{path} must hold a JSON object')
        inject = data.get('inject_time')
        return cls(
            folder=path.parent,
            case_id=str(data.get('case_id') or path.parent.name),
            fault_start_s=None if inject is None else float(inject),
            candidate_services=_strings(data.get('candidate_services')),
            candidate_indicators=_strings(data.get('candidate_indicators')),
        )


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError('Expected a list of names')
    return tuple(str(item) for item in value)


class Budget:
    """Tool, turn, cost and time counters with a verification reserve.

    While the reserve is held, investigation sees only the limits minus the
    reserve, so a run always reaches verify with capacity left for it.
    """

    def __init__(
        self,
        limits: config.Limits,
        *,
        reserve: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the clock and hold the reserve when verify is on."""
        self.limits = limits
        self.reserve_held = reserve
        self.tool_calls = 0
        self.model_turns = 0
        self.cost_usd = Decimal(0)
        self._clock = clock
        self._started = clock()

    def reservation_ok(self) -> bool:
        """Return True when the limits leave investigation something to use.

        Returns:
            False when the reserve would take every tool call, turn or
            dollar, so investigation could not start.
        """
        if not self.reserve_held:
            return self.limits.tool_calls > 0 and self.limits.model_turns > 0
        return (
            self.limits.tool_calls > self.limits.verify_tool_calls
            and self.limits.model_turns > self.limits.verify_model_turns
            and self.limits.cost_usd > self.limits.verify_cost_usd
        )

    def release_reserve(self) -> None:
        """Make the reserved capacity available, on entering verify."""
        self.reserve_held = False

    def tool_calls_left(self) -> int:
        """Return tool calls available to the current step."""
        held = self.limits.verify_tool_calls if self.reserve_held else 0
        return max(0, self.limits.tool_calls - held - self.tool_calls)

    def model_turns_left(self) -> int:
        """Return model turns available to the current step."""
        held = self.limits.verify_model_turns if self.reserve_held else 0
        return max(0, self.limits.model_turns - held - self.model_turns)

    def cost_left(self) -> Decimal:
        """Return settled-cost headroom available to the current step."""
        held = self.limits.verify_cost_usd if self.reserve_held else 0
        return max(Decimal(0), self.limits.cost_usd - held - self.cost_usd)

    def elapsed_s(self) -> float:
        """Return seconds since the budget was created."""
        return self._clock() - self._started

    def charge_tool_call(self) -> bool:
        """Count one tool call, or return False when none is left."""
        if self.tool_calls_left() <= 0:
            return False
        self.tool_calls += 1
        return True

    def charge_turn(self, cost_usd: Decimal) -> None:
        """Count one settled model call."""
        self.model_turns += 1
        self.cost_usd += cost_usd

    def stop_reason(self) -> StopReason | None:
        """Return why no further model call may start, or None."""
        if self.elapsed_s() >= self.limits.wall_time_s:
            return 'wall_time'
        if self.model_turns_left() <= 0:
            return 'model_turn_limit'
        if self.cost_left() <= 0:
            return 'cost_cap'
        return None

    def to_dict(self) -> dict[str, object]:
        """Return the counters for checkpoints and reports."""
        return {
            'tool_calls': self.tool_calls,
            'model_turns': self.model_turns,
            'cost_usd': str(self.cost_usd),
            'elapsed_s': round(self.elapsed_s(), 3),
            'reserve_held': self.reserve_held,
            'limits': self.limits.to_dict(),
        }


class LoopBudget(Protocol):
    """What the tool loop needs from a budget.

    `Budget` (single mode) and `RoleAllowance` (one role in multi mode)
    both satisfy it.
    """

    def stop_reason(self) -> StopReason | None:
        """Return why no further model call may start, or None."""
        ...

    def tool_calls_left(self) -> int:
        """Return tool calls available to the current step."""
        ...

    def charge_tool_call(self) -> bool:
        """Count one tool call, or return False when none is left."""
        ...

    def charge_turn(self, cost_usd: Decimal) -> None:
        """Count one settled model call."""
        ...

    def to_dict(self) -> dict[str, object]:
        """Return the counters for checkpoints and reports."""
        ...


@dataclasses.dataclass
class _Counts:
    tool_calls: int = 0
    model_turns: int = 0
    cost_usd: Decimal = Decimal(0)

    def to_dict(self) -> dict[str, object]:
        return {
            'tool_calls': self.tool_calls,
            'model_turns': self.model_turns,
            'cost_usd': str(self.cost_usd),
        }


class SharedBudget:
    """One run's limits in multi mode, split into role allowances.

    The totals are the single agent's `Limits`. Each role draws on an
    allowance granted from the pool; a grant never exceeds what the pool
    holds, so the roles together can never exceed the limits. Closing an
    allowance returns what it did not use to the pool. When the returned
    capacity is handed out again is the graph's choice (Phase 07 grants it
    only after the fan-out, so results do not depend on finishing order).

    Every method is safe to call from several threads.
    """

    def __init__(
        self,
        limits: config.Limits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the clock with nothing granted."""
        self.limits = limits
        self.used = _Counts()
        self._open: list[RoleAllowance] = []
        self._lock = threading.RLock()
        self._clock = clock
        self._started = clock()

    @property
    def lock(self) -> threading.RLock:
        """Return the lock that guards every counter of this budget."""
        return self._lock

    def now(self) -> float:
        """Return the clock reading."""
        return self._clock()

    def elapsed_s(self) -> float:
        """Return seconds since the budget was created."""
        return self._clock() - self._started

    def pool(self) -> _Counts:
        """Return capacity neither used nor promised to an open allowance."""
        with self._lock:
            promised = _Counts()
            for allowance in self._open:
                left = allowance.unused()
                promised.tool_calls += left.tool_calls
                promised.model_turns += left.model_turns
                promised.cost_usd += left.cost_usd
            return _Counts(
                max(
                    0,
                    self.limits.tool_calls
                    - self.used.tool_calls
                    - promised.tool_calls,
                ),
                max(
                    0,
                    self.limits.model_turns
                    - self.used.model_turns
                    - promised.model_turns,
                ),
                max(
                    Decimal(0),
                    self.limits.cost_usd
                    - self.used.cost_usd
                    - promised.cost_usd,
                ),
            )

    def allocate(
        self,
        role: str,
        *,
        tool_calls: int | None = None,
        model_turns: int | None = None,
        cost_usd: Decimal | None = None,
        wall_time_s: float | None = None,
        hypothesis_id: str | None = None,
    ) -> 'RoleAllowance':
        """Grant an allowance from the pool.

        Args:
            role: Role tag the allowance is for.
            tool_calls: Tool calls wanted; None takes all the pool has.
            model_turns: Model turns wanted; None takes all the pool has.
            cost_usd: Cost wanted; None takes all the pool has.
            wall_time_s: Seconds this role may run, from now; None leaves
                only the run's wall-time limit.
            hypothesis_id: Hypothesis a tester works on.

        Returns:
            The allowance; each amount is the smaller of the request and
            the pool.

        Raises:
            ValueError: A requested amount is negative.
        """
        for value in (tool_calls, model_turns, cost_usd):
            if value is not None and value < 0:
                raise ValueError('Allowance amounts must be nonnegative')
        with self._lock:
            pool = self.pool()
            granted = _Counts(
                pool.tool_calls
                if tool_calls is None
                else min(tool_calls, pool.tool_calls),
                pool.model_turns
                if model_turns is None
                else min(model_turns, pool.model_turns),
                pool.cost_usd
                if cost_usd is None
                else min(cost_usd, pool.cost_usd),
            )
            allowance = RoleAllowance(
                self, role, granted, wall_time_s, hypothesis_id
            )
            self._open.append(allowance)
            return allowance

    def top_up(self, allowance: 'RoleAllowance') -> None:
        """Move everything in the pool into an open allowance.

        Args:
            allowance: An open allowance of this budget.

        Raises:
            ValueError: The allowance is closed or belongs elsewhere.
        """
        with self._lock:
            if allowance not in self._open:
                raise ValueError('Only an open allowance can be topped up')
            pool = self.pool()
            allowance.granted.tool_calls += pool.tool_calls
            allowance.granted.model_turns += pool.model_turns
            allowance.granted.cost_usd += pool.cost_usd

    def release(self, allowance: 'RoleAllowance') -> None:
        """Forget a closed allowance, so its unused share is in the pool.

        Args:
            allowance: The allowance, already marked closed.
        """
        with self._lock:
            if allowance in self._open:
                self._open.remove(allowance)

    def to_dict(self) -> dict[str, object]:
        """Return the run totals for checkpoints and reports."""
        with self._lock:
            return {
                **self.used.to_dict(),
                'elapsed_s': round(self.elapsed_s(), 3),
                'pool': self.pool().to_dict(),
                'limits': self.limits.to_dict(),
            }


class RoleAllowance:
    """One role's share of a `SharedBudget`; a `LoopBudget` for the loop.

    Every charge counts against the allowance and the run totals at once,
    under the shared lock. A closed allowance has nothing left.
    """

    def __init__(
        self,
        shared: SharedBudget,
        role: str,
        granted: _Counts,
        wall_time_s: float | None,
        hypothesis_id: str | None,
    ) -> None:
        """Wrap a grant; use `SharedBudget.allocate` to make one."""
        self.shared = shared
        self.role = role
        self.hypothesis_id = hypothesis_id
        self.granted = granted
        self.used = _Counts()
        self.wall_time_s = wall_time_s
        self.closed = False
        self._started = shared.now()

    def unused(self) -> _Counts:
        """Return what is granted and not yet used."""
        with self.shared.lock:
            if self.closed:
                return _Counts()
            return _Counts(
                max(0, self.granted.tool_calls - self.used.tool_calls),
                max(0, self.granted.model_turns - self.used.model_turns),
                max(Decimal(0), self.granted.cost_usd - self.used.cost_usd),
            )

    def elapsed_s(self) -> float:
        """Return seconds since the allowance was granted."""
        return self.shared.now() - self._started

    def tool_calls_left(self) -> int:
        """Return tool calls this role may still make."""
        with self.shared.lock:
            run_left = self.shared.limits.tool_calls - (
                self.shared.used.tool_calls
            )
            return max(0, min(self.unused().tool_calls, run_left))

    def model_turns_left(self) -> int:
        """Return model turns this role may still make."""
        with self.shared.lock:
            run_left = self.shared.limits.model_turns - (
                self.shared.used.model_turns
            )
            return max(0, min(self.unused().model_turns, run_left))

    def cost_left(self) -> Decimal:
        """Return settled-cost headroom this role still has."""
        with self.shared.lock:
            run_left = self.shared.limits.cost_usd - self.shared.used.cost_usd
            return max(Decimal(0), min(self.unused().cost_usd, run_left))

    def charge_tool_call(self) -> bool:
        """Count one tool call, or return False when none is left."""
        with self.shared.lock:
            if self.tool_calls_left() <= 0:
                return False
            self.used.tool_calls += 1
            self.shared.used.tool_calls += 1
            return True

    def charge_turn(self, cost_usd: Decimal) -> None:
        """Count one settled model call, here and in the run totals."""
        with self.shared.lock:
            self.used.model_turns += 1
            self.used.cost_usd += cost_usd
            self.shared.used.model_turns += 1
            self.shared.used.cost_usd += cost_usd

    def stop_reason(self) -> StopReason | None:
        """Return why this role may not start another model call, or None."""
        if self.shared.elapsed_s() >= self.shared.limits.wall_time_s:
            return 'wall_time'
        if self.wall_time_s is not None and (
            self.elapsed_s() >= self.wall_time_s
        ):
            return 'role_wall_time'
        if self.model_turns_left() <= 0:
            return 'model_turn_limit'
        if self.cost_left() <= 0:
            return 'cost_cap'
        return None

    def close(self) -> None:
        """Stop the allowance and return what it did not use to the pool."""
        with self.shared.lock:
            self.closed = True
            self.shared.release(self)

    def to_dict(self) -> dict[str, object]:
        """Return this role's counters and the run totals."""
        with self.shared.lock:
            data: dict[str, object] = {
                'role': self.role,
                'granted': self.granted.to_dict(),
                'used': self.used.to_dict(),
                'elapsed_s': round(self.elapsed_s(), 3),
                'closed': self.closed,
                'run': self.shared.to_dict(),
            }
            if self.hypothesis_id is not None:
                data['hypothesis_id'] = self.hypothesis_id
            return data
