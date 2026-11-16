"""The hypothesis ledger and the citation validity check, enforced in code.

A citation is valid when it names a successful, non-empty data-tool call
from this run, a locator found in that result, and a quote that appears in
it. Validity says a claim points at something real; whether the observation
supports the claim is a separate, manual audit (Phase 06).
"""

import dataclasses
from collections.abc import Collection
from collections.abc import Mapping
from typing import Literal

from incidents_agent.agent import report
from incidents_agent.agent import results

type Status = Literal['open', 'supported', 'rejected']
type Origin = Literal['data', 'memory']

_STATUS_RANK = {'supported': 0, 'open': 1, 'rejected': 2}
_VERIFY_RANK = {
    'supported': 0,
    'inconclusive': 1,
    'unverified': 2,
    'refuted': 3,
}
_CONFIDENCE_RANK = {'high': 0, 'medium': 1, 'low': 2}
_MAX_TOKEN_HINTS = 8

UPDATE_TOOL_NAME = 'update_hypotheses'

_EVIDENCE_SCHEMA: dict[str, object] = {
    'type': 'array',
    'description': (
        'Observations from earlier tool calls in this run. Each needs the '
        'call_id, a locator from that result (table, column, chunk id or '
        'component name) and a value or phrase copied exactly from it.'
    ),
    'items': {
        'type': 'object',
        'properties': {
            'call_id': {'type': 'string'},
            'locator': {'type': 'string'},
            'quote': {'type': 'string'},
            'stance': {'type': 'string', 'enum': ['for', 'against']},
        },
        'required': ['call_id', 'locator', 'quote'],
    },
}

UPDATE_TOOL_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'operations': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'op': {
                        'type': 'string',
                        'enum': ['add', 'revise', 'reject'],
                    },
                    'id': {
                        'type': 'string',
                        'description': 'Hypothesis id, for revise and reject.',
                    },
                    'statement': {'type': 'string'},
                    'component': {'type': 'string'},
                    'fault_type': {'type': 'string'},
                    'status': {
                        'type': 'string',
                        'enum': ['open', 'supported'],
                    },
                    'confidence': {
                        'type': 'string',
                        'enum': list(report.CONFIDENCE_LEVELS),
                    },
                    'origin': {'type': 'string', 'enum': ['data', 'memory']},
                    'reason': {
                        'type': 'string',
                        'description': 'Why, for reject.',
                    },
                    'evidence': _EVIDENCE_SCHEMA,
                },
                'required': ['op'],
            },
        }
    },
    'required': ['operations'],
}


def check_citation(
    item: object,
    store: results.ResultStore,
    run_id: str,
    *,
    default_stance: Literal['for', 'against'] = 'for',
) -> tuple[report.EvidenceRef | None, str | None]:
    """Validate one evidence item against the results of this run.

    Args:
        item: The evidence object the model supplied.
        store: Full results of this run's data-tool calls.
        run_id: The current run.
        default_stance: Stance when the item does not give one.

    Returns:
        The validated reference and None, or None and an error the model
        can read.
    """
    if not isinstance(item, dict):
        return None, 'evidence items must be objects'
    call_id, locator, quote = (
        item.get('call_id'),
        item.get('locator'),
        item.get('quote'),
    )
    if (
        not isinstance(call_id, str)
        or not isinstance(locator, str)
        or not isinstance(quote, str)
    ):
        return None, 'evidence needs string call_id, locator and quote'
    stored = store.get(call_id)
    if stored is None:
        return None, f'{call_id}: no data-tool call with this id in this run'
    if stored.run_id != run_id:
        return None, f'{call_id}: the call belongs to another run'
    if not stored.ok:
        return None, f'{call_id}: the call failed, so it cannot be cited'
    if results.is_empty(stored.result):
        return None, f'{call_id}: the call returned an empty result'
    tokens = results.locator_tokens(stored)
    if locator not in tokens:
        hints = ', '.join(sorted(tokens)[:_MAX_TOKEN_HINTS])
        return None, (
            f'{call_id}: locator {locator!r} is not in that result; '
            f'use one of: {hints}'
        )
    if not results.contains_quote(stored.result, quote):
        return None, (
            f'{call_id}: quote {quote!r} does not appear in that result; '
            'copy the value exactly, or re-read it with read_result'
        )
    stance = item.get('stance', default_stance)
    return (
        report.EvidenceRef(
            call_id, locator, quote, 'against' if stance == 'against' else 'for'
        ),
        None,
    )


@dataclasses.dataclass
class Hypothesis:
    """One ledger entry.

    Attributes:
        id: Ledger id (`H1`, `H2`, ...).
        statement: What is claimed.
        component: Suspected component, service or indicator.
        fault_type: Fault category, when stated.
        status: Open, supported or rejected.
        confidence: Qualitative level.
        origin: `data`, or `memory` when the idea came from a past incident.
        verification: Outcome of the disproof check.
        verification_check: The check that produced the outcome.
        evidence_for: Validated supporting evidence.
        evidence_against: Validated contrary evidence.
        reject_reason: Why it was rejected.
    """

    id: str
    statement: str
    component: str
    fault_type: str | None = None
    status: Status = 'open'
    confidence: report.Confidence = 'low'
    origin: Origin = 'data'
    verification: report.Verification = 'unverified'
    verification_check: str | None = None
    evidence_for: list[report.EvidenceRef] = dataclasses.field(
        default_factory=list
    )
    evidence_against: list[report.EvidenceRef] = dataclasses.field(
        default_factory=list
    )
    reject_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        data = dataclasses.asdict(self)
        data['evidence_for'] = [item.to_dict() for item in self.evidence_for]
        data['evidence_against'] = [
            item.to_dict() for item in self.evidence_against
        ]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'Hypothesis':
        """Rebuild an entry written by `to_dict`."""
        return cls(
            id=str(data['id']),
            statement=str(data['statement']),
            component=str(data['component']),
            fault_type=_optional(data.get('fault_type')),
            status=_pick(data.get('status'), ('open', 'supported', 'rejected')),
            confidence=_pick(data.get('confidence'), report.CONFIDENCE_LEVELS),
            origin=_pick(data.get('origin'), ('data', 'memory')),
            verification=_pick(
                data.get('verification'),
                ('unverified', 'supported', 'refuted', 'inconclusive'),
            ),
            verification_check=_optional(data.get('verification_check')),
            evidence_for=_evidence(data.get('evidence_for')),
            evidence_against=_evidence(data.get('evidence_against')),
            reject_reason=_optional(data.get('reject_reason')),
        )

    def summary(self) -> dict[str, object]:
        """Return the compact form shown to the model."""
        return {
            'id': self.id,
            'component': self.component,
            'statement': self.statement,
            'status': self.status,
            'confidence': self.confidence,
            'verification': self.verification,
            'origin': self.origin,
            'evidence_for': len(self.evidence_for),
            'evidence_against': len(self.evidence_against),
        }


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _pick[T: str](value: object, options: tuple[T, ...]) -> T:
    for option in options:
        if value == option:
            return option
    return options[0]


def _evidence(value: object) -> list[report.EvidenceRef]:
    if not isinstance(value, list):
        return []
    return [
        report.EvidenceRef.from_dict(item)
        for item in value
        if isinstance(item, dict)
    ]


class HypothesisLedger:
    """Typed working state the model edits through `update_hypotheses`."""

    def __init__(self, run_id: str, store: results.ResultStore) -> None:
        """Start an empty ledger checked against `store`."""
        self.run_id = run_id
        self.store = store
        self.entries: dict[str, Hypothesis] = {}

    def has_data_support(self, entry: Hypothesis) -> bool:
        """Return True when supporting evidence comes from current data."""
        for item in entry.evidence_for:
            stored = self.store.get(item.call_id)
            if stored is not None and not stored.from_memory:
                return True
        return False

    def apply(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """Run the `update_hypotheses` tool.

        Each operation is applied on its own. Evidence items that fail the
        citation check are dropped with an error; the rest of the operation
        still applies.

        Args:
            arguments: The tool arguments, `{"operations": [...]}`.

        Returns:
            Tool result: `ok`, `applied`, `errors` and the ledger summary.
        """
        operations = arguments.get('operations')
        errors: list[str] = []
        applied: list[str] = []
        if not isinstance(operations, list) or not operations:
            errors.append('operations must be a non-empty list')
            operations = []
        for index, operation in enumerate(operations):
            prefix = f'operations[{index}]'
            if not isinstance(operation, dict):
                errors.append(f'{prefix}: must be an object')
                continue
            kind = operation.get('op')
            if kind == 'add':
                done, problems = self._add(operation)
            elif kind == 'revise':
                done, problems = self._revise(operation)
            elif kind == 'reject':
                done, problems = self._reject(operation)
            else:
                done, problems = None, ['op must be add, revise or reject']
            errors.extend(f'{prefix}: {problem}' for problem in problems)
            if done is not None:
                applied.append(done)
        return {
            'ok': bool(applied) and not errors,
            'applied': applied,
            'errors': errors,
            'hypotheses': [entry.summary() for entry in self.ranked()],
        }

    def _citations(
        self,
        items: object,
        stance: Literal['for', 'against'],
        problems: list[str],
    ) -> list[report.EvidenceRef]:
        if items is None:
            return []
        if not isinstance(items, list):
            problems.append('evidence must be a list')
            return []
        valid: list[report.EvidenceRef] = []
        for item in items:
            ref, error = check_citation(
                item, self.store, self.run_id, default_stance=stance
            )
            if ref is None:
                problems.append(f'evidence rejected: {error}')
            else:
                valid.append(ref)
        return valid

    def _attach(
        self, entry: Hypothesis, refs: list[report.EvidenceRef]
    ) -> None:
        for ref in refs:
            target = (
                entry.evidence_against
                if ref.stance == 'against'
                else entry.evidence_for
            )
            if ref not in target:
                target.append(ref)

    def _set_fields(
        self, entry: Hypothesis, op: Mapping[str, object], problems: list[str]
    ) -> None:
        for name in ('statement', 'fault_type'):
            value = op.get(name)
            if isinstance(value, str) and value.strip():
                setattr(entry, name, value.strip())
        confidence = op.get('confidence')
        if confidence is not None:
            if confidence in report.CONFIDENCE_LEVELS:
                entry.confidence = _pick(confidence, report.CONFIDENCE_LEVELS)
            else:
                problems.append('confidence must be low, medium or high')
        status = op.get('status')
        if status is None:
            return
        if status == 'open':
            entry.status = 'open'
            entry.reject_reason = None
        elif status == 'supported':
            if self.has_data_support(entry):
                entry.status = 'supported'
            elif entry.origin == 'memory':
                problems.append(
                    f'{entry.id} came from memory and needs evidence from '
                    'the current data before it can be supported'
                )
            else:
                problems.append(
                    f'{entry.id} needs at least one valid supporting '
                    'citation before it can be supported'
                )
        else:
            problems.append('status must be open or supported')

    def _add(self, op: Mapping[str, object]) -> tuple[str | None, list[str]]:
        problems: list[str] = []
        statement, component = op.get('statement'), op.get('component')
        if not isinstance(statement, str) or not statement.strip():
            return None, ['add needs a statement']
        if not isinstance(component, str) or not component.strip():
            return None, ['add needs a component']
        existing = self.find(component)
        if existing is not None:
            return None, [
                f'{existing.id} already covers {component!r}; revise it'
            ]
        entry = Hypothesis(
            id=f'H{len(self.entries) + 1}',
            statement=statement.strip(),
            component=component.strip(),
            origin='memory' if op.get('origin') == 'memory' else 'data',
        )
        self._attach(
            entry, self._citations(op.get('evidence'), 'for', problems)
        )
        self.entries[entry.id] = entry
        self._set_fields(entry, op, problems)
        return f'added {entry.id}', problems

    def _entry(
        self, op: Mapping[str, object]
    ) -> tuple[Hypothesis | None, str | None]:
        entry = self.entries.get(str(op.get('id')))
        if entry is None:
            known = ', '.join(self.entries) or 'none yet'
            return None, f'unknown hypothesis id {op.get("id")!r} ({known})'
        return entry, None

    def _revise(self, op: Mapping[str, object]) -> tuple[str | None, list[str]]:
        entry, error = self._entry(op)
        if entry is None:
            return None, [str(error)]
        problems: list[str] = []
        self._attach(
            entry, self._citations(op.get('evidence'), 'for', problems)
        )
        self._set_fields(entry, op, problems)
        return f'revised {entry.id}', problems

    def _reject(self, op: Mapping[str, object]) -> tuple[str | None, list[str]]:
        entry, error = self._entry(op)
        if entry is None:
            return None, [str(error)]
        reason = op.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            return None, ['reject needs a reason']
        problems: list[str] = []
        self._attach(
            entry, self._citations(op.get('evidence'), 'against', problems)
        )
        entry.status = 'rejected'
        entry.reject_reason = reason.strip()
        return f'rejected {entry.id}', problems

    def record_verification(
        self,
        hypothesis_id: str,
        outcome: report.Verification,
        check: str,
        evidence: object,
        fresh_calls: Collection[str],
    ) -> list[str]:
        """Store the outcome of a disproof check on one hypothesis.

        `supported` and `refuted` need at least one valid citation of a call
        made during the check (`fresh_calls`). A refuted hypothesis is
        rejected. An `inconclusive` outcome needs no evidence.

        Args:
            hypothesis_id: Ledger id of the hypothesis checked.
            outcome: `supported`, `refuted` or `inconclusive`.
            check: What was checked and why it could disprove the hypothesis.
            evidence: Evidence items from the check.
            fresh_calls: Call ids made during the check.

        Returns:
            Errors; empty when the outcome was recorded.
        """
        entry = self.entries.get(hypothesis_id)
        if entry is None:
            return [f'unknown hypothesis id {hypothesis_id!r}']
        problems: list[str] = []
        stance: Literal['for', 'against'] = (
            'against' if outcome == 'refuted' else 'for'
        )
        refs = self._citations(evidence, stance, problems)
        if outcome in ('supported', 'refuted') and not any(
            ref.call_id in fresh_calls for ref in refs
        ):
            return [
                *problems,
                f'a {outcome} outcome must cite a tool call made during '
                'this check',
            ]
        self._attach(entry, refs)
        entry.verification = outcome
        entry.verification_check = check
        if outcome == 'refuted':
            entry.status = 'rejected'
            entry.reject_reason = f'refuted by verification: {check}'
        elif outcome == 'supported' and self.has_data_support(entry):
            entry.status = 'supported'
        return []

    def find(self, component: str) -> Hypothesis | None:
        """Return the non-rejected entry for a component, if any."""
        wanted = component.strip().casefold()
        for entry in self.entries.values():
            if (
                entry.component.casefold() == wanted
                and entry.status != 'rejected'
            ):
                return entry
        return None

    def ranked(self) -> list[Hypothesis]:
        """Return entries in a deterministic rank order.

        Supported before open before rejected; then by verification
        outcome, confidence, supporting evidence (more first), contrary
        evidence (fewer first) and creation order.

        Returns:
            Entries, best first.
        """
        order = list(self.entries)
        return sorted(
            self.entries.values(),
            key=lambda entry: (
                _STATUS_RANK[entry.status],
                _VERIFY_RANK[entry.verification],
                _CONFIDENCE_RANK[entry.confidence],
                -len(entry.evidence_for),
                len(entry.evidence_against),
                order.index(entry.id),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the ledger for checkpoints."""
        return {
            'run_id': self.run_id,
            'entries': [entry.to_dict() for entry in self.entries.values()],
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, object], store: results.ResultStore
    ) -> 'HypothesisLedger':
        """Rebuild a ledger written by `to_dict`, over its result store."""
        ledger = cls(str(data['run_id']), store)
        entries = data.get('entries')
        for item in entries if isinstance(entries, list) else []:
            if isinstance(item, dict):
                entry = Hypothesis.from_dict(item)
                ledger.entries[entry.id] = entry
        return ledger
