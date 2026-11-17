"""The multi-agent graph (Phase 07): lead, parallel testers, skeptic.

orient -> lead -> testers (fan-out, in parallel) -> merge -> skeptic ->
lead_report -> report. A tester works on one hypothesis with a clean
conversation and returns a verdict; only the merge step writes verdicts
to the ledger, in hypothesis-id order. The skeptic sees only the leading
hypothesis and its cited results. If it disproves the hypothesis, the
lead's report step becomes a correction, once.

Every role draws on an allowance from one `SharedBudget` whose totals are
the single agent's limits. The skeptic's and the report's shares are
granted before the fan-out; what testers leave unused goes to the report.
"""

import dataclasses
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated
from typing import Protocol
from typing import TypedDict

from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph
from langgraph.types import Send

from incidents_agent.agent import context
from incidents_agent.agent import investigate
from incidents_agent.agent import prompts
from incidents_agent.agent import roles
from incidents_agent.agent import run_state
from incidents_agent.agent import state
from incidents_agent.llm import contracts

# Stops after which no further role may run: the run's clock or the spend
# guard ran out, or the lead could not start.
_RUN_STOPS = frozenset({'wall_time', 'spend_refused', 'reservation_refused'})
_FALLBACK_CONFIRM = '(fallback brief: the lead did not dispatch) new support'
_FALLBACK_REFUTE = '(fallback brief: the lead did not dispatch) contrary data'


@dataclasses.dataclass
class MultiRun:
    """The multi-mode additions to a `RunState`.

    Attributes:
        run: The common run state; its conversation is the lead's.
        shared: The run's budget, split into role allowances.
        lead: The lead's first-pass allowance.
        skeptic: The skeptic's allowance, granted before the fan-out.
        report: The lead's report allowance, granted before the fan-out.
        briefs: Briefs sent to the testers.
        testers: Tester allowances by hypothesis id.
        verdicts: Merged verdicts by hypothesis id.
        challenge: The skeptic's accepted outcome, if any.
    """

    run: run_state.RunState
    shared: state.SharedBudget
    lead: state.RoleAllowance
    skeptic: state.RoleAllowance
    report: state.RoleAllowance
    briefs: tuple[roles.TesterBrief, ...] = ()
    testers: dict[str, state.RoleAllowance] = dataclasses.field(
        default_factory=dict
    )
    verdicts: dict[str, roles.Verdict] = dataclasses.field(default_factory=dict)
    challenge: dict[str, object] | None = None

    @classmethod
    def start(
        cls, run: run_state.RunState, shared: state.SharedBudget
    ) -> 'MultiRun':
        """Grant the skeptic's, the report's and the lead's shares.

        The skeptic and the report are granted first, so the lead and the
        testers can never use them up.

        Args:
            run: The run.
            shared: The run's budget.

        Returns:
            The multi-mode run state.
        """
        split = run.session.config.multi
        skeptic_cost = split.reserve_cost_usd / 2
        skeptic = shared.allocate(
            'skeptic',
            tool_calls=split.skeptic_tool_calls,
            model_turns=split.skeptic_model_turns,
            cost_usd=skeptic_cost,
        )
        report = shared.allocate(
            'lead',
            tool_calls=split.report_tool_calls,
            model_turns=split.report_model_turns,
            cost_usd=split.reserve_cost_usd - skeptic_cost,
        )
        lead = shared.allocate(
            'lead',
            tool_calls=split.lead_tool_calls,
            model_turns=split.lead_model_turns,
        )
        return cls(run, shared, lead, skeptic, report)

    def role_session(
        self, allowance: state.RoleAllowance, *, model_alias: str = ''
    ) -> investigate.Session:
        """Return a session for one role, sharing tools, ledger and record.

        Args:
            allowance: The role's budget.
            model_alias: Model for this role; empty keeps the run's.

        Returns:
            A copy of the run's session drawing on `allowance`.
        """
        base = self.run.session
        settings = (
            dataclasses.replace(base.config, model_alias=model_alias)
            if model_alias
            else base.config
        )
        return dataclasses.replace(
            base, budget=allowance, config=settings, stated_ranks=()
        )

    def checkpoint(self, node: str) -> None:
        """Write the checkpoint with the multi-mode fields."""
        self.run.extra = {
            'mode': 'multi',
            'budget': self.shared.to_dict(),
            'briefs': [brief.to_dict() for brief in self.briefs],
            'tester_verdicts': {
                key: value.to_dict() for key, value in self.verdicts.items()
            },
            'challenge': self.challenge,
        }
        self.run.checkpoint(node)


class _GraphState(TypedDict):
    node: str
    verdicts: Annotated[dict[str, roles.Verdict], roles.merge_verdicts]


class _TesterInput(TypedDict):
    brief: roles.TesterBrief


class _Step(Protocol):
    def __call__(self, state: _GraphState) -> dict[str, object]: ...


def _orient(multi: MultiRun) -> None:
    run = multi.run
    split = run.session.config.multi
    lead = multi.lead
    if (
        lead.tool_calls_left() <= 0
        or lead.model_turns_left() <= 0
        or lead.cost_left() <= 0
    ):
        run.stop_reason = 'reservation_refused'
        return
    run.conversation.append(
        contracts.Message(
            'user',
            prompts.LEAD_DISPATCH_PROMPT.format(
                opening=run.opening,
                tool_calls=lead.tool_calls_left(),
                max_testers=split.max_testers,
            ),
        )
    )


def _fallback_briefs(multi: MultiRun) -> tuple[roles.TesterBrief, ...]:
    book = multi.run.session.ledger
    if book is None:
        return ()
    live = [entry for entry in book.ranked() if entry.status != 'rejected']
    limit = multi.run.session.config.multi.max_testers
    return tuple(
        roles.TesterBrief(entry.id, _FALLBACK_CONFIRM, _FALLBACK_REFUTE)
        for entry in live[:limit]
    )


def _lead(multi: MultiRun) -> None:
    run = multi.run
    session = multi.role_session(multi.lead)
    outcome = investigate.investigate(
        session,
        investigate.Brief(node='lead', role='lead', system=prompts.LEAD_SYSTEM),
        run.conversation,
        roles.dispatch_tool(session, run.session.config.multi.max_testers),
    )
    multi.lead.close()
    run.session.stated_ranks = session.stated_ranks
    run.stop_reason = outcome.stop_reason
    accepted = outcome.accepted
    if accepted is not None and accepted['outcome'] == 'no_fault_found':
        run.submission = {'outcome': 'no_fault_found', 'ranks': []}
        return
    if outcome.stop_reason in state.HARD_STOPS:
        return
    multi.briefs = (
        roles.briefs_from(accepted)
        if accepted is not None
        else _fallback_briefs(multi)
    )
    _grant_testers(multi)
    run.session.record.record(
        'dispatch',
        role='lead',
        node='lead',
        fallback=accepted is None,
        briefs=[brief.to_dict() for brief in multi.briefs],
    )


def _grant_testers(multi: MultiRun) -> None:
    if not multi.briefs:
        return
    split = multi.run.session.config.multi
    cost_each = multi.shared.pool().cost_usd / Decimal(len(multi.briefs))
    for brief in multi.briefs:
        multi.testers[brief.hypothesis_id] = multi.shared.allocate(
            'tester',
            tool_calls=split.tester_tool_calls,
            model_turns=split.tester_model_turns,
            cost_usd=cost_each,
            wall_time_s=split.tester_wall_time_s,
            hypothesis_id=brief.hypothesis_id,
        )


def _route_after_lead(multi: MultiRun) -> list[Send] | str:
    if multi.run.submission is not None or not multi.briefs:
        return 'report'
    return [Send('tester', {'brief': brief}) for brief in multi.briefs]


def _tester(multi: MultiRun, brief: roles.TesterBrief) -> roles.Verdict:
    run = multi.run
    hid = brief.hypothesis_id
    allowance = multi.testers[hid]
    session = multi.role_session(allowance)
    book = run.session.ledger
    entry = None if book is None else book.entries.get(hid)
    session.record.record(
        'node', role='tester', hypothesis_id=hid, node='tester', phase='enter'
    )
    conversation = context.Conversation(
        run.session.config.context, session.executor.store
    )
    conversation.append(
        contracts.Message(
            'user',
            prompts.TESTER_BRIEF_PROMPT.format(
                hypothesis_id=hid,
                component='?' if entry is None else entry.component,
                statement='' if entry is None else entry.statement,
                confirm_if=brief.confirm_if,
                refute_if=brief.refute_if,
                tool_calls=allowance.tool_calls_left(),
            ),
        )
    )
    outcome = investigate.investigate(
        session,
        investigate.Brief(
            node='tester',
            role='tester',
            hypothesis_id=hid,
            system=prompts.TESTER_SYSTEM,
            tools=roles.role_tools(session, 'tester'),
        ),
        conversation,
        roles.verdict_tool(session, hid),
    )
    found = (
        None if outcome.accepted is None else outcome.accepted['verdict_record']
    )
    verdict = (
        dataclasses.replace(found, stop_reason=outcome.stop_reason)
        if isinstance(found, roles.Verdict)
        else roles.undecided(hid, outcome.stop_reason)
    )
    session.record.record(
        'verdict', role='tester', node='tester', **verdict.to_dict()
    )
    return verdict


def _merge(multi: MultiRun, verdicts: dict[str, roles.Verdict]) -> None:
    run = multi.run
    for allowance in multi.testers.values():
        allowance.close()
    multi.verdicts = dict(verdicts)
    book = run.session.ledger
    errors: dict[str, list[str]] = {}
    for hid in sorted(verdicts, key=roles.id_order):
        verdict = verdicts[hid]
        if book is not None:
            problems = book.apply_verdict(
                hid,
                verdict.verdict,
                confidence=verdict.confidence,
                evidence=list(verdict.evidence),
                reason=verdict.reason,
            )
            if problems:
                errors[hid] = problems
        if verdict.stop_reason in _RUN_STOPS:
            run.stop_reason = verdict.stop_reason
    run.session.record.record(
        'ledger',
        role='lead',
        node='merge',
        verdicts=[
            verdicts[hid].to_dict()
            for hid in sorted(verdicts, key=roles.id_order)
        ],
        errors=errors,
        ledger=None if book is None else book.to_dict(),
    )


def _route_after_merge(multi: MultiRun) -> str:
    run = multi.run
    if run.stop_reason in _RUN_STOPS:
        return 'report'
    if run_state.target(run) is None:
        return 'lead_report'
    return 'skeptic'


def _skeptic(multi: MultiRun) -> None:
    run = multi.run
    target = run_state.target(run)
    book = run.session.ledger
    if target is None or book is None:
        return
    hid, component = target
    settings = run.session.config
    session = multi.role_session(
        multi.skeptic,
        model_alias=settings.skeptic_model_alias or settings.model_alias,
    )
    conversation = context.Conversation(
        settings.context, session.executor.store
    )
    direction = (
        prompts.DIRECTION_HINT.format(component=component)
        if run.catalog_has_dependencies
        else ''
    )
    conversation.append(
        contracts.Message(
            'user',
            roles.skeptic_opening(
                book.entries[hid],
                session.executor.store,
                tool_calls=multi.skeptic.tool_calls_left(),
                direction=direction,
            ),
        )
    )
    outcome = investigate.investigate(
        session,
        investigate.Brief(
            node='skeptic',
            role='skeptic',
            system=prompts.SKEPTIC_SYSTEM,
            tools=roles.role_tools(session, 'skeptic'),
        ),
        conversation,
        roles.challenge_tool(session, hid),
    )
    multi.skeptic.close()
    run.verify_runs += 1
    accepted = outcome.accepted
    multi.challenge = {
        'hypothesis_id': hid,
        'component': component,
        'outcome': None if accepted is None else accepted['outcome'],
        'verification': (
            'unverified' if accepted is None else accepted['verification']
        ),
        'check': None if accepted is None else accepted['check'],
        'stop_reason': outcome.stop_reason,
    }
    session.record.record(
        'verification', role='skeptic', node='skeptic', **multi.challenge
    )
    if outcome.stop_reason in _RUN_STOPS:
        run.stop_reason = outcome.stop_reason
    if (
        accepted is not None
        and accepted['outcome'] == 'disproved'
        and not run.corrected
    ):
        run.pending_correction = {
            'target': hid,
            'component': component,
            'check': str(accepted['check']),
        }


def _route_after_skeptic(multi: MultiRun) -> str:
    return 'report' if multi.run.stop_reason in _RUN_STOPS else 'lead_report'


def _ledger_text(multi: MultiRun) -> str:
    book = multi.run.session.ledger
    if book is None:
        return '(no ledger)'
    return (
        '\n'.join(f'- {entry.summary()}' for entry in book.ranked())
        or '(empty)'
    )


def _skeptic_text(multi: MultiRun) -> str:
    challenge = multi.challenge
    if challenge is None:
        return 'no claim had support from the data, so none was challenged'
    return (
        f'{challenge["hypothesis_id"]} ({challenge["component"]}): '
        f'{challenge["outcome"] or "no outcome"}; {challenge["check"] or ""}'
    )


def _lead_report(multi: MultiRun) -> None:
    run = multi.run
    multi.shared.top_up(multi.report)
    tool_calls = multi.report.tool_calls_left()
    if run.pending_correction is not None:
        run.corrected = True
        text = prompts.LEAD_CORRECTION_PROMPT.format(
            **run.pending_correction,
            ledger=_ledger_text(multi),
            tool_calls=tool_calls,
        )
        run.pending_correction = None
    else:
        text = prompts.LEAD_REPORT_PROMPT.format(
            ledger=_ledger_text(multi),
            skeptic=_skeptic_text(multi),
            tool_calls=tool_calls,
        )
    run.conversation.append(contracts.Message('user', text))
    session = multi.role_session(multi.report)
    outcome = investigate.investigate(
        session,
        investigate.Brief(
            node='lead_report', role='lead', system=prompts.LEAD_SYSTEM
        ),
        run.conversation,
        investigate.submit_tool(session),
    )
    multi.report.close()
    if session.stated_ranks:
        run.session.stated_ranks = session.stated_ranks
    if outcome.accepted is not None:
        run.submission = outcome.accepted
    run.stop_reason = outcome.stop_reason


def _report(multi: MultiRun) -> None:
    run_state.finish(multi.run, counters=multi.shared.to_dict())


def _serial(
    name: str,
    multi: MultiRun,
    action: Callable[[MultiRun], None],
    role: str = 'lead',
) -> _Step:
    # The parameter must be called `state` to match LangGraph's node type.
    def step(state: _GraphState) -> dict[str, object]:
        del state  # Run data lives in `multi`.
        multi.run.session.record.record(
            'node', role=role, node=name, phase='enter'
        )
        action(multi)
        multi.checkpoint(name)
        return {'node': name}

    return step


def build_graph(
    run: run_state.RunState,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], None]:
    """Compile the multi-agent graph over one run and return its runner.

    Args:
        run: The run; its session's config must be in multi mode, and its
            conversation becomes the lead's.
        clock: Monotonic clock for the shared budget.

    Returns:
        A function that runs the graph from `orient` to `report`.
    """
    settings = run.session.config
    multi = MultiRun.start(
        run, state.SharedBudget(settings.limits, clock=clock)
    )

    def tester(state: _TesterInput) -> dict[str, object]:
        verdict = _tester(multi, state['brief'])
        return {'verdicts': {verdict.hypothesis_id: verdict}}

    def merge(state: _GraphState) -> dict[str, object]:
        multi.run.session.record.record(
            'node', role='lead', node='merge', phase='enter'
        )
        _merge(multi, state['verdicts'])
        multi.checkpoint('merge')
        return {'node': 'merge'}

    builder = StateGraph(_GraphState)
    builder.add_node('orient', _serial('orient', multi, _orient))
    builder.add_node('lead', _serial('lead', multi, _lead))
    builder.add_node('tester', tester)
    builder.add_node('merge', merge)
    builder.add_node(
        'skeptic', _serial('skeptic', multi, _skeptic, role='skeptic')
    )
    builder.add_node('lead_report', _serial('lead_report', multi, _lead_report))
    builder.add_node('report', _serial('report', multi, _report))
    builder.add_edge(START, 'orient')
    builder.add_conditional_edges(
        'orient',
        lambda _: (
            'report' if run.stop_reason == 'reservation_refused' else 'lead'
        ),
        ['lead', 'report'],
    )
    builder.add_conditional_edges(
        'lead', lambda _: _route_after_lead(multi), ['tester', 'report']
    )
    builder.add_edge('tester', 'merge')
    builder.add_conditional_edges(
        'merge',
        lambda _: _route_after_merge(multi),
        ['skeptic', 'lead_report', 'report'],
    )
    builder.add_conditional_edges(
        'skeptic',
        lambda _: _route_after_skeptic(multi),
        ['lead_report', 'report'],
    )
    builder.add_edge('lead_report', 'report')
    builder.add_edge('report', END)
    graph = builder.compile()

    def invoke() -> None:
        graph.invoke(
            {'node': 'start', 'verdicts': {}},
            config={'max_concurrency': settings.multi.max_testers + 1},
        )

    return invoke
