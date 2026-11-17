"""Multi-agent roles (Phase 07): briefs, verdicts and their terminal tools.

Each role ends its step with its own terminal tool, validated in code:
the lead's `dispatch`, a tester's `record_verdict` and the skeptic's
`record_challenge`. Verdicts and challenges count only with evidence from
calls the role made itself, found in the trajectory by role and
hypothesis id.
"""

import dataclasses
import json
from collections.abc import Mapping
from typing import Literal

from incidents_agent.agent import executor
from incidents_agent.agent import investigate
from incidents_agent.agent import ledger
from incidents_agent.agent import prompts
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.llm import contracts

DISPATCH_NAME = 'dispatch'
VERDICT_NAME = 'record_verdict'
CHALLENGE_NAME = 'record_challenge'

type VerdictKind = Literal['supported', 'rejected', 'undecided']
type ChallengeOutcome = Literal['holds', 'disproved']

_VERDICTS: tuple[VerdictKind, ...] = ('supported', 'rejected', 'undecided')
_CHALLENGES: tuple[ChallengeOutcome, ...] = ('holds', 'disproved')

BRIEF_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'hypothesis_id': {'type': 'string'},
        'confirm_if': {
            'type': 'string',
            'description': 'What observation would confirm it.',
        },
        'refute_if': {
            'type': 'string',
            'description': 'What observation would refute it.',
        },
    },
    'required': ['hypothesis_id', 'confirm_if', 'refute_if'],
}

DISPATCH_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'outcome': {
            'type': 'string',
            'enum': ['dispatch', 'no_fault_found'],
        },
        'briefs': {'type': 'array', 'items': BRIEF_SCHEMA},
    },
    'required': ['outcome'],
}

VERDICT_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'hypothesis_id': {'type': 'string'},
        'verdict': {'type': 'string', 'enum': list(_VERDICTS)},
        'confidence': {
            'type': 'string',
            'enum': list(report.CONFIDENCE_LEVELS),
        },
        'reason': {'type': 'string'},
        'evidence': ledger.EVIDENCE_SCHEMA,
    },
    'required': ['hypothesis_id', 'verdict'],
}

CHALLENGE_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'hypothesis_id': {'type': 'string'},
        'check': {
            'type': 'string',
            'description': 'What you checked and why it could disprove it.',
        },
        'outcome': {'type': 'string', 'enum': list(_CHALLENGES)},
        'evidence': ledger.EVIDENCE_SCHEMA,
    },
    'required': ['hypothesis_id', 'check', 'outcome'],
}


@dataclasses.dataclass(frozen=True)
class TesterBrief:
    """What the lead hands one tester.

    Attributes:
        hypothesis_id: Ledger id of the hypothesis to test.
        confirm_if: Observation that would confirm it.
        refute_if: Observation that would refute it.
    """

    hypothesis_id: str
    confirm_if: str
    refute_if: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Verdict:
    """A tester's finding on its hypothesis.

    Attributes:
        hypothesis_id: The hypothesis tested.
        verdict: `supported`, `rejected` or `undecided`.
        confidence: Qualitative level.
        evidence: Validated citations of the tester's own calls.
        reason: Why, mainly for `undecided`.
        stop_reason: How the tester's step ended.
    """

    hypothesis_id: str
    verdict: VerdictKind
    confidence: report.Confidence = 'low'
    evidence: tuple[report.EvidenceRef, ...] = ()
    reason: str | None = None
    stop_reason: str = 'submitted'

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return {
            'hypothesis_id': self.hypothesis_id,
            'verdict': self.verdict,
            'confidence': self.confidence,
            'evidence': [item.to_dict() for item in self.evidence],
            'reason': self.reason,
            'stop_reason': self.stop_reason,
        }


def merge_verdicts(
    left: Mapping[str, Verdict], right: Mapping[str, Verdict]
) -> dict[str, Verdict]:
    """Combine verdicts keyed by hypothesis id; the graph's reducer.

    Args:
        left: Verdicts gathered so far.
        right: Verdicts from one tester.

    Returns:
        Both sets together.

    Raises:
        ValueError: Two different verdicts claim the same hypothesis.
    """
    merged = dict(left)
    for key, value in right.items():
        if key in merged and merged[key] != value:
            raise ValueError(f'Two different verdicts for {key}')
        merged[key] = value
    return merged


def id_order(hypothesis_id: str) -> tuple[int, str]:
    """Return a sort key that puts `H2` before `H10`."""
    return len(hypothesis_id), hypothesis_id


def undecided(hypothesis_id: str, stop_reason: str) -> Verdict:
    """Return the verdict for a tester that ended without one."""
    return Verdict(
        hypothesis_id,
        'undecided',
        reason=f'tester stopped without a verdict: {stop_reason}',
        stop_reason=stop_reason,
    )


def role_tools(
    session: investigate.Session, role: str
) -> frozenset[str] | None:
    """Return the tools a role may call besides its terminal tool.

    The lead may call every tool, including the ledger and, when memory is
    on, `search_past_incidents`. Testers and the skeptic get the data
    tools (with `dependency_graph`) and `read_result`, never the ledger or
    memory.

    Args:
        session: The run.
        role: `lead`, `tester` or `skeptic`.

    Returns:
        Allowed tool names, or None for every tool.
    """
    if role == 'lead':
        return None
    data = session.executor.names() - results.MEMORY_TOOLS
    return frozenset({*data, executor.READ_RESULT_NAME})


def own_calls(
    session: investigate.Session, role: str, hypothesis_id: str | None
) -> set[str]:
    """Return the data-tool calls a role made in this continuation.

    Args:
        session: The run.
        role: Role tag on the calls.
        hypothesis_id: The tester's hypothesis, or None for other roles.

    Returns:
        Call ids.
    """
    record = session.record
    return {
        str(event['call_id'])
        for event in record.of_type('tool_call')
        if event.get('role') == role
        and event.get('hypothesis_id') == hypothesis_id
        and event.get('continuation') == record.continuation
    }


def _own_evidence(
    session: investigate.Session,
    items: object,
    stance: Literal['for', 'against'],
    own: set[str],
) -> tuple[list[report.EvidenceRef], list[str], bool]:
    if items is None:
        return [], [], False
    if not isinstance(items, list):
        return [], ['evidence must be a list'], False
    refs: list[report.EvidenceRef] = []
    errors: list[str] = []
    for item in items:
        ref, error = ledger.check_citation(
            item,
            session.executor.store,
            session.run_id,
            default_stance=stance,
        )
        if ref is None:
            errors.append(f'evidence rejected: {error}')
        else:
            refs.append(dataclasses.replace(ref, stance=stance))
    has_own = any(
        ref.call_id in own
        and (stored := session.executor.store.get(ref.call_id)) is not None
        and not stored.from_memory
        for ref in refs
    )
    return refs, errors, has_own


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ''


def dispatch_tool(
    session: investigate.Session, max_testers: int
) -> investigate.TerminalTool:
    """Return the lead's `dispatch` tool and its validator.

    Args:
        session: The run, for its ledger.
        max_testers: Most briefs allowed.

    Returns:
        The terminal tool for the lead's first pass.
    """
    book = session.ledger

    def validate(
        args: dict[str, object],
    ) -> tuple[dict[str, object] | None, list[str]]:
        outcome = args.get('outcome')
        raw = args.get('briefs')
        briefs = raw if isinstance(raw, list) else []
        if raw is not None and not isinstance(raw, list):
            return None, ['briefs must be a list']
        if outcome == 'no_fault_found':
            if briefs:
                return None, ['no_fault_found takes no briefs']
            if book is not None and any(
                entry.status == 'supported' for entry in book.entries.values()
            ):
                return None, [
                    'a supported hypothesis exists; reject it before '
                    'reporting no_fault_found'
                ]
            return {'outcome': 'no_fault_found', 'briefs': []}, []
        if outcome != 'dispatch':
            return None, ['outcome must be "dispatch" or "no_fault_found"']
        if not 1 <= len(briefs) <= max_testers:
            return None, [f'dispatch needs 1 to {max_testers} briefs']
        errors: list[str] = []
        accepted: list[dict[str, object]] = []
        seen: set[str] = set()
        for index, item in enumerate(briefs):
            prefix = f'briefs[{index}]'
            if not isinstance(item, dict):
                errors.append(f'{prefix}: must be an object')
                continue
            hid = _text(item.get('hypothesis_id'))
            entry = None if book is None else book.entries.get(hid)
            if entry is None:
                known = '' if book is None else ', '.join(book.entries)
                errors.append(
                    f'{prefix}: unknown hypothesis {hid!r}; add it to the '
                    f'ledger first (known: {known or "none"})'
                )
            elif entry.status == 'rejected':
                errors.append(f'{prefix}: {hid} is rejected')
            elif hid in seen:
                errors.append(f'{prefix}: {hid} is briefed twice')
            confirm, refute = (
                _text(item.get('confirm_if')),
                _text(item.get('refute_if')),
            )
            if not confirm or not refute:
                errors.append(f'{prefix}: needs confirm_if and refute_if')
            seen.add(hid)
            accepted.append(
                {
                    'hypothesis_id': hid,
                    'confirm_if': confirm,
                    'refute_if': refute,
                }
            )
        if errors:
            return None, errors
        return {'outcome': 'dispatch', 'briefs': accepted}, []

    return investigate.TerminalTool(
        contracts.Tool(
            DISPATCH_NAME,
            'Send one brief per ledger hypothesis to the testers, or end '
            'with "no_fault_found" when the data shows nothing abnormal.',
            executor.json_schema(DISPATCH_SCHEMA),
        ),
        validate,
    )


def briefs_from(accepted: Mapping[str, object]) -> tuple[TesterBrief, ...]:
    """Return the briefs in an accepted `dispatch` call."""
    raw = accepted.get('briefs')
    return tuple(
        TesterBrief(
            str(item['hypothesis_id']),
            str(item['confirm_if']),
            str(item['refute_if']),
        )
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict)
    )


def verdict_tool(
    session: investigate.Session, hypothesis_id: str
) -> investigate.TerminalTool:
    """Return a tester's `record_verdict` tool and its validator.

    `supported` and `rejected` need at least one valid citation of a call
    this tester made; `undecided` needs none.

    Args:
        session: The tester's session.
        hypothesis_id: The only hypothesis it may rule on.

    Returns:
        The terminal tool; accepted arguments carry a `Verdict` under
        `verdict_record`.
    """

    def validate(
        args: dict[str, object],
    ) -> tuple[dict[str, object] | None, list[str]]:
        if args.get('hypothesis_id') != hypothesis_id:
            return None, [f'hypothesis_id must be {hypothesis_id!r}']
        kind = report.pick(args.get('verdict'), _VERDICTS)
        if kind is None:
            return None, ['verdict must be supported, rejected or undecided']
        confidence = args.get('confidence', 'low')
        level = report.pick(confidence, report.CONFIDENCE_LEVELS)
        if level is None:
            return None, ['confidence must be low, medium or high']
        stance: Literal['for', 'against'] = (
            'against' if kind == 'rejected' else 'for'
        )
        own = own_calls(session, 'tester', hypothesis_id)
        refs, errors, has_own = _own_evidence(
            session, args.get('evidence'), stance, own
        )
        if kind != 'undecided' and not has_own:
            return None, [
                *errors,
                f'a {kind} verdict must cite a tool call you made',
            ]
        verdict = Verdict(
            hypothesis_id,
            kind,
            level,
            tuple(refs),
            _text(args.get('reason')) or None,
        )
        return {**args, 'verdict_record': verdict}, []

    return investigate.TerminalTool(
        contracts.Tool(
            VERDICT_NAME,
            'Record your verdict on the hypothesis, with evidence from the '
            'calls you made.',
            executor.json_schema(VERDICT_SCHEMA),
        ),
        validate,
    )


def challenge_tool(
    session: investigate.Session, hypothesis_id: str
) -> investigate.TerminalTool:
    """Return the skeptic's `record_challenge` tool and its validator.

    The outcome is stored on the ledger as a verification: `disproved`
    becomes `refuted` (the hypothesis is rejected) and needs a citation of
    the skeptic's own call; `holds` becomes `supported` with such a
    citation and `inconclusive` without one.

    Args:
        session: The skeptic's session.
        hypothesis_id: The claim under challenge.

    Returns:
        The terminal tool; accepted arguments carry the ledger outcome
        under `verification`.
    """

    def validate(
        args: dict[str, object],
    ) -> tuple[dict[str, object] | None, list[str]]:
        if args.get('hypothesis_id') != hypothesis_id:
            return None, [f'hypothesis_id must be {hypothesis_id!r}']
        outcome = report.pick(args.get('outcome'), _CHALLENGES)
        check = _text(args.get('check'))
        if outcome is None:
            return None, ['outcome must be holds or disproved']
        if not check:
            return None, ['check must say what was checked']
        own = own_calls(session, 'skeptic', None)
        stance: Literal['for', 'against'] = (
            'against' if outcome == 'disproved' else 'for'
        )
        _, errors, has_own = _own_evidence(
            session, args.get('evidence'), stance, own
        )
        if outcome == 'disproved' and not has_own:
            return None, [
                *errors,
                'a disproved outcome must cite a tool call you made',
            ]
        verification: report.Verification = (
            'refuted'
            if outcome == 'disproved'
            else 'supported'
            if has_own
            else 'inconclusive'
        )
        if session.ledger is not None:
            problems = session.ledger.record_verification(
                hypothesis_id, verification, check, args.get('evidence'), own
            )
            if problems:
                return None, problems
        return {
            **args,
            'outcome': outcome,
            'check': check,
            'verification': verification,
        }, []

    return investigate.TerminalTool(
        contracts.Tool(
            CHALLENGE_NAME,
            'Record whether the claim holds or is disproved, with evidence '
            'from the calls you made for this check.',
            executor.json_schema(CHALLENGE_SCHEMA),
        ),
        validate,
    )


def skeptic_opening(
    entry: ledger.Hypothesis,
    store: results.ResultStore,
    *,
    tool_calls: int,
    direction: str,
) -> str:
    """Return the skeptic's only input: the claim and its cited results.

    Nothing the lead or the testers wrote is included: only the ledger
    statement, the citations and the full stored results they point at.

    Args:
        entry: The leading hypothesis.
        store: The run's result store.
        tool_calls: Tool calls the skeptic has.
        direction: Optional hint that dependency data exists.

    Returns:
        The first and only user message of the skeptic's conversation.
    """
    blocks = []
    for ref in entry.evidence_for:
        stored = store.get(ref.call_id)
        if stored is not None and stored.from_memory:
            continue
        body = (
            '(not found)'
            if stored is None
            else json.dumps(
                {'tool': stored.tool, 'args': dict(stored.args)},
                ensure_ascii=False,
            )
            + '\n'
            + executor.result_text(stored.result)
        )
        blocks.append(f'- {ref.call_id}, {ref.locator}: "{ref.quote}"\n{body}')
    return prompts.SKEPTIC_CHALLENGE_PROMPT.format(
        hypothesis_id=entry.id,
        component=entry.component,
        statement=entry.statement,
        evidence='\n'.join(blocks) or '(none)',
        tool_calls=tool_calls,
        direction=f'\n\n{direction}' if direction else '',
    )
