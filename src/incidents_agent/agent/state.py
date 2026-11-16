"""The task an agent is given and the budget it spends while working."""

import dataclasses
import json
import time
from collections.abc import Callable
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

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
]

# Stops that end the run outright. Any other stop still lets verify use the
# capacity reserved for it.
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
