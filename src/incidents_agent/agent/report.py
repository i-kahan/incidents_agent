"""The structured report every run ends with, however it stops."""

import dataclasses
from collections.abc import Mapping
from typing import Literal

type Outcome = Literal[
    'verified', 'provisional', 'inconclusive', 'no_fault_found'
]
type Verification = Literal[
    'supported', 'refuted', 'inconclusive', 'unverified'
]
type Confidence = Literal['low', 'medium', 'high']
type Risk = Literal['read_only', 'reversible', 'disruptive']

OUTCOMES: tuple[Outcome, ...] = (
    'verified',
    'provisional',
    'inconclusive',
    'no_fault_found',
)
CONFIDENCE_LEVELS: tuple[Confidence, ...] = ('low', 'medium', 'high')
RISK_ORDER: tuple[Risk, ...] = ('read_only', 'reversible', 'disruptive')
VERIFICATIONS: tuple[Verification, ...] = (
    'supported',
    'refuted',
    'inconclusive',
    'unverified',
)


@dataclasses.dataclass(frozen=True)
class EvidenceRef:
    """A validated pointer to an observation in this run.

    Attributes:
        call_id: Tool call the observation came from.
        locator: Table, column, chunk id or window inside that result.
        quote: Value or phrase that appears in the result.
        stance: `for` or `against` the hypothesis.
    """

    call_id: str
    locator: str
    quote: str
    stance: Literal['for', 'against'] = 'for'

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'EvidenceRef':
        """Rebuild an item written by `to_dict`."""
        stance = data.get('stance', 'for')
        return cls(
            str(data['call_id']),
            str(data['locator']),
            str(data['quote']),
            'against' if stance == 'against' else 'for',
        )


@dataclasses.dataclass(frozen=True)
class RankedCause:
    """One root-cause candidate in the final ranking.

    Attributes:
        rank: 1 for the top cause.
        component: Suspected component, service or indicator.
        statement: What is claimed about it.
        hypothesis_id: Ledger entry it came from, if the ledger was on.
        verification: Outcome of the disproof check for this cause.
        confidence: Qualitative level.
        confidence_reasons: Why that level.
        evidence: Validated evidence for and against.
        cited: False when no valid citation from the current data backs
            the cause: a name ranked without one, or any cause with the
            ledger off. Such a cause never decides the outcome.
    """

    rank: int
    component: str
    statement: str
    hypothesis_id: str | None
    verification: Verification
    confidence: Confidence
    confidence_reasons: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    cited: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return {
            'rank': self.rank,
            'component': self.component,
            'statement': self.statement,
            'hypothesis_id': self.hypothesis_id,
            'verification': self.verification,
            'confidence': self.confidence,
            'confidence_reasons': list(self.confidence_reasons),
            'evidence': [item.to_dict() for item in self.evidence],
            'cited': self.cited,
        }


@dataclasses.dataclass(frozen=True)
class Action:
    """A recommended action; never executed by the agent.

    Attributes:
        hypothesis_id: Hypothesis (or component, ledger off) it addresses.
        action: What to do.
        risk: Read-only check, reversible change or disruptive change.
        how_to_verify: How to tell whether it worked.
    """

    hypothesis_id: str
    action: str
    risk: Risk
    how_to_verify: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class DataRequest:
    """Specific additional data that would settle an open hypothesis.

    Attributes:
        hypothesis_id: Open or undecided ledger entry it is derived from.
        what: The data wanted.
        source: Where it would come from.
        effect: How it would confirm or refute the hypothesis.
    """

    hypothesis_id: str
    what: str
    source: str
    effect: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Report:
    """The final, validated output of a run.

    Attributes:
        run_id: Run identifier.
        continuation: 0 for the first pass, then 1, 2, ...
        outcome: Verified, provisional, inconclusive or no fault found.
        stop_reason: Why the run ended.
        fault_type: Fault category, when stated.
        ranks: Component names in rank order, for prediction files.
        causes: Ranked causes with evidence.
        actions: Recommended actions, read-only checks first.
        data_requests: Data that would settle an unsettled diagnosis.
        dropped: Claims, actions or requests removed by validation.
        counters: Budget counters at the end of the run.
        prompt_hash: Hash of the prompt module used.
        ledger_enabled: Whether the hypothesis ledger was on.
        verify_enabled: Whether the verify step was on.
    """

    run_id: str
    continuation: int
    outcome: Outcome
    stop_reason: str
    fault_type: str | None
    ranks: tuple[str, ...]
    causes: tuple[RankedCause, ...]
    actions: tuple[Action, ...]
    data_requests: tuple[DataRequest, ...]
    dropped: tuple[str, ...]
    counters: Mapping[str, object]
    prompt_hash: str
    ledger_enabled: bool = True
    verify_enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return {
            'run_id': self.run_id,
            'continuation': self.continuation,
            'outcome': self.outcome,
            'stop_reason': self.stop_reason,
            'fault_type': self.fault_type,
            'ranks': list(self.ranks),
            'causes': [cause.to_dict() for cause in self.causes],
            'actions': [action.to_dict() for action in self.actions],
            'data_requests': [item.to_dict() for item in self.data_requests],
            'dropped': list(self.dropped),
            'counters': dict(self.counters),
            'prompt_hash': self.prompt_hash,
            'ledger_enabled': self.ledger_enabled,
            'verify_enabled': self.verify_enabled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'Report':
        """Rebuild a report written by `to_dict`.

        Args:
            data: Mapping produced by `to_dict`.

        Returns:
            The report.

        Raises:
            ValueError: A field has an unknown value.
        """
        outcome = _choice(data['outcome'], OUTCOMES)
        fault = data.get('fault_type')
        counters = data.get('counters') or {}
        return cls(
            run_id=str(data['run_id']),
            continuation=int(str(data.get('continuation', 0))),
            outcome=outcome,
            stop_reason=str(data['stop_reason']),
            fault_type=None if fault is None else str(fault),
            ranks=_strs(data.get('ranks')),
            causes=tuple(_cause(item) for item in _list(data.get('causes'))),
            actions=tuple(
                Action(
                    str(item['hypothesis_id']),
                    str(item['action']),
                    _choice(item['risk'], RISK_ORDER),
                    str(item['how_to_verify']),
                )
                for item in _list(data.get('actions'))
            ),
            data_requests=tuple(
                DataRequest(
                    str(item['hypothesis_id']),
                    str(item['what']),
                    str(item['source']),
                    str(item['effect']),
                )
                for item in _list(data.get('data_requests'))
            ),
            dropped=_strs(data.get('dropped')),
            counters=dict(counters) if isinstance(counters, dict) else {},
            prompt_hash=str(data.get('prompt_hash', '')),
            ledger_enabled=bool(data.get('ledger_enabled', True)),
            verify_enabled=bool(data.get('verify_enabled', True)),
        )


def _list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strs(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def pick[T: str](value: object, options: tuple[T, ...]) -> T | None:
    """Return the option equal to `value`, or None."""
    for option in options:
        if value == option:
            return option
    return None


def _choice[T: str](value: object, options: tuple[T, ...]) -> T:
    chosen = pick(value, options)
    if chosen is None:
        raise ValueError(f'{value!r} is not one of {options}')
    return chosen


def _cause(data: Mapping[str, object]) -> RankedCause:
    hypothesis = data.get('hypothesis_id')
    evidence = data.get('evidence')
    reasons = data.get('confidence_reasons')
    return RankedCause(
        rank=int(str(data['rank'])),
        component=str(data['component']),
        statement=str(data.get('statement', '')),
        hypothesis_id=None if hypothesis is None else str(hypothesis),
        verification=_choice(data['verification'], VERIFICATIONS),
        confidence=_choice(data['confidence'], CONFIDENCE_LEVELS),
        confidence_reasons=_strs(reasons),
        evidence=tuple(
            EvidenceRef.from_dict(item)
            for item in (evidence if isinstance(evidence, list) else [])
            if isinstance(item, dict)
        ),
        cited=data.get('cited', True) is not False,
    )
