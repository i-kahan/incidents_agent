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
    'description': (
        'Name a ledger hypothesis by hypothesis_id, or give component and '
        'statement (and any evidence) to add a new one with this brief.'
    ),
    'properties': {
        'hypothesis_id': {'type': 'string'},
        'component': {'type': 'string'},
        'statement': {'type': 'string'},
        'evidence': ledger.EVIDENCE_SCHEMA,
        'confirm_if': {
            'type': 'string',
            'description': 'What observation would confirm it.',
        },
        'refute_if': {
            'type': 'string',
            'description': 'What observation would refute it.',
        },
    },
    'required': ['confirm_if', 'refute_if'],
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
        'alternative_component': {
            'type': 'string',
            'description': (
                'For "disproved": the affected component the claimed one '
                'calls, whose trouble started first.'
            ),
        },
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


def _plan_briefs(
    book: ledger.HypothesisLedger | None, briefs: list[object]
) -> tuple[list[dict[str, object]], list[str]]:
    """Check every brief before the ledger changes; resolve known ids."""
    errors: list[str] = []
    planned: list[dict[str, object]] = []
    seen: dict[str, dict[str, object]] = {}
    for index, item in enumerate(briefs):
        prefix = f'briefs[{index}]'
        if not isinstance(item, dict):
            errors.append(f'{prefix}: must be an object')
            continue
        confirm = _text(item.get('confirm_if'))
        refute = _text(item.get('refute_if'))
        if not confirm or not refute:
            errors.append(f'{prefix}: needs confirm_if and refute_if')
        hid = _text(item.get('hypothesis_id'))
        component = _text(item.get('component'))
        statement = _text(item.get('statement'))
        entry = None
        if book is not None and hid:
            entry = book.entries.get(hid)
            if entry is None:
                known = ', '.join(book.entries) or 'none'
                errors.append(
                    f'{prefix}: unknown hypothesis {hid!r} (known: {known}); '
                    'or give component and statement to add one'
                )
                continue
        elif book is not None and component:
            entry = book.find(component)
            if entry is None and not statement:
                errors.append(f'{prefix}: a new hypothesis needs a statement')
                continue
        else:
            errors.append(
                f'{prefix}: give hypothesis_id, or component and statement'
            )
            continue
        if entry is not None and entry.status == 'rejected':
            errors.append(f'{prefix}: {entry.id} is rejected')
            continue
        key = entry.id if entry is not None else component.casefold()
        earlier = seen.get(key)
        if earlier is not None:
            # One hypothesis per component: merge a second brief for the
            # same component into the first instead of refusing both.
            _merge_brief(
                earlier, statement, item.get('evidence'), confirm, refute
            )
            continue
        brief: dict[str, object] = {
            'hypothesis_id': None if entry is None else entry.id,
            'component': component,
            'statement': statement,
            'evidence': item.get('evidence'),
            'confirm_if': confirm,
            'refute_if': refute,
        }
        seen[key] = brief
        planned.append(brief)
    return planned, errors


def _merge_brief(
    brief: dict[str, object],
    statement: str,
    evidence: object,
    confirm: str,
    refute: str,
) -> None:
    """Fold a second brief for the same hypothesis into the first."""

    def joined(name: str, extra: str) -> str:
        first = str(brief[name])
        return first if not extra or extra == first else f'{first}; or {extra}'

    brief['statement'] = joined('statement', statement)
    brief['confirm_if'] = joined('confirm_if', confirm)
    brief['refute_if'] = joined('refute_if', refute)
    items = [
        *(brief['evidence'] if isinstance(brief['evidence'], list) else []),
        *(evidence if isinstance(evidence, list) else []),
    ]
    brief['evidence'] = items or None


def _add_new(
    book: ledger.HypothesisLedger, planned: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[str]]:
    """Add the briefs' new hypotheses to the ledger and return the briefs.

    Invalid evidence is dropped by the ledger's own citation check; its
    errors are returned, as `update_hypotheses` would report them.
    """
    accepted: list[dict[str, object]] = []
    errors: list[str] = []
    for brief in planned:
        hid = brief['hypothesis_id']
        if hid is None:
            operation: dict[str, object] = {
                'op': 'add',
                'component': brief['component'],
                'statement': brief['statement'],
            }
            if brief['evidence'] is not None:
                operation['evidence'] = brief['evidence']
            result = book.apply({'operations': [operation]})
            raw = result.get('errors')
            errors.extend(
                str(e) for e in (raw if isinstance(raw, list) else [])
            )
            entry = book.find(str(brief['component']))
            if entry is None:
                continue
            hid = entry.id
        accepted.append(
            {
                'hypothesis_id': hid,
                'confirm_if': brief['confirm_if'],
                'refute_if': brief['refute_if'],
            }
        )
    return accepted, errors


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
        if not briefs:
            return None, [f'dispatch needs 1 to {max_testers} briefs']
        planned, errors = _plan_briefs(book, briefs)
        if not errors and len(planned) > max_testers:
            errors = [
                f'dispatch needs 1 to {max_testers} briefs (after merging '
                'briefs for the same component)'
            ]
        if errors or book is None:
            return None, errors or ['dispatch needs the hypothesis ledger']
        accepted, ledger_errors = _add_new(book, planned)
        return {
            'outcome': 'dispatch',
            'briefs': accepted,
            'ledger_errors': ledger_errors,
        }, []

    return investigate.TerminalTool(
        contracts.Tool(
            DISPATCH_NAME,
            'Send one brief per hypothesis to the testers, naming a ledger '
            'hypothesis or adding a new one in the brief (this does not use '
            'a tool call), or end with "no_fault_found" when the data shows '
            'nothing abnormal.',
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
    the skeptic's own call that shows the `alternative_component` it
    names: a different component, and a candidate when there is a list.
    `holds` becomes `supported` with a citation of the skeptic's own call
    and `inconclusive` without one.

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
        refs, errors, has_own = _own_evidence(
            session, args.get('evidence'), stance, own
        )
        if outcome == 'disproved' and not has_own:
            return None, [
                *errors,
                'a disproved outcome must cite a tool call you made',
            ]
        if outcome == 'disproved':
            problems = _alternative_errors(
                session, hypothesis_id, args, refs, own
            )
            if problems:
                return None, problems
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


def _alternative_errors(
    session: investigate.Session,
    hypothesis_id: str,
    args: Mapping[str, object],
    refs: list[report.EvidenceRef],
    own: set[str],
) -> list[str]:
    """Check that a disproval names the earlier cause it points to.

    The alternative must be a different component (a candidate, when the
    task has a candidate list) and must appear in a result of a call the
    skeptic made and cited.
    """
    alternative = _text(args.get('alternative_component'))
    if not alternative:
        return [
            'a disproved outcome must name alternative_component: the '
            'affected component the claimed one calls, whose trouble '
            'started first'
        ]
    wanted = alternative.casefold()
    entry = (
        None
        if session.ledger is None
        else session.ledger.entries.get(hypothesis_id)
    )
    if entry is not None and entry.component.casefold() == wanted:
        return ['alternative_component must differ from the claimed one']
    candidates = {name.casefold() for name in session.task.candidate_services}
    if candidates and wanted not in candidates:
        return [f'{alternative!r} is not in the candidate list']
    for ref in refs:
        stored = session.executor.store.get(ref.call_id)
        if ref.call_id not in own or stored is None:
            continue
        named = {name.casefold() for name in stored.target_components}
        if wanted in named or wanted in ref.quote.casefold():
            return []
    return [f'cite a call you made whose result shows {alternative!r}']


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
