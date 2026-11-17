"""The tool-calling loop, shared by investigate, verify and Phase 07 roles.

`investigate` is a function of a session (tools, ledger, budget, record),
a brief, a conversation and a terminal tool. Phase 07's hypothesis testers
reuse it with a different brief and terminal tool.
"""

import dataclasses
import json
from collections.abc import Callable
from collections.abc import Iterable

from incidents_agent.agent import config
from incidents_agent.agent import context
from incidents_agent.agent import executor
from incidents_agent.agent import ledger
from incidents_agent.agent import prompts
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.llm import contracts

SUBMIT_NAME = 'submit'

type Validator = Callable[
    [dict[str, object]], tuple[dict[str, object] | None, list[str]]
]


@dataclasses.dataclass
class Session:
    """What every step of one run shares.

    Attributes:
        run: Spend-guard run identity.
        task: The task.
        config: Agent configuration.
        client: Model client (guarded or fake).
        executor: Data-tool executor over the run's result store.
        ledger: Hypothesis ledger, or None when ablated.
        budget: Limits and counters: a `Budget` in single mode, a role's
            `RoleAllowance` in multi mode.
        record: Trajectory.
        briefing: Stable catalog briefing from `orient`.
        stated_ranks: Last ranking the model stated in any submit call,
            used when the ledger is off and the run is forced to end.
    """

    run: contracts.Run
    task: state.Task
    config: config.AgentConfig
    client: contracts.ModelClient
    executor: executor.ToolExecutor
    ledger: ledger.HypothesisLedger | None
    budget: state.LoopBudget
    record: trajectory.Trajectory
    briefing: str = ''
    stated_ranks: tuple[str, ...] = ()

    @property
    def run_id(self) -> str:
        """Return the run id."""
        return self.run.id

    def tools(
        self,
        terminal: contracts.Tool,
        allowed: frozenset[str] | None = None,
    ) -> tuple[contracts.Tool, ...]:
        """Return the tool list sent with every request of a step.

        The list never changes inside a step, whatever a tool result says,
        so the cached prompt prefix stays valid.

        Args:
            terminal: The tool that ends the step; always included.
            allowed: Names of the other tools this step may use; None
                allows them all.

        Returns:
            Data tools, agent tools and the terminal tool, in that order.
        """
        agent_tools = [executor.READ_RESULT_TOOL]
        if self.ledger is not None:
            agent_tools.insert(
                0,
                contracts.Tool(
                    ledger.UPDATE_TOOL_NAME,
                    'Add, revise or reject hypotheses in the ledger, with '
                    'cited evidence.',
                    executor.json_schema(ledger.UPDATE_TOOL_SCHEMA),
                ),
            )
        listed = (*self.executor.tools, *agent_tools)
        if allowed is not None:
            listed = tuple(tool for tool in listed if tool.name in allowed)
        return (*listed, terminal)


@dataclasses.dataclass(frozen=True)
class TerminalTool:
    """The tool that ends a step, and the code that validates its call."""

    tool: contracts.Tool
    validate: Validator


@dataclasses.dataclass(frozen=True)
class Brief:
    """How one step runs the loop.

    Attributes:
        node: Graph node name, recorded on every event.
        role: Role tag, recorded on every event (Phase 07).
        max_turns: Extra cap on model turns for this step.
        hypothesis_id: Hypothesis a tester works on, recorded on every
            event when set.
        system: System prompt for this step; None uses the agent's.
        tools: Names of the non-terminal tools this step may call; None
            allows every tool. A call to a tool outside it is refused.
    """

    node: str = 'investigate'
    role: str = 'agent'
    max_turns: int | None = None
    hypothesis_id: str | None = None
    system: str | None = None
    tools: frozenset[str] | None = None


@dataclasses.dataclass(frozen=True)
class LoopResult:
    """How a step ended.

    Attributes:
        stop_reason: `submitted`, or the limit or failure that stopped it.
        accepted: Validated arguments of the terminal call, if accepted.
        calls_made: Data-tool call ids made during the step.
        invalid_calls: Terminal calls refused by validation.
    """

    stop_reason: state.StopReason
    accepted: dict[str, object] | None
    calls_made: tuple[str, ...]
    invalid_calls: int


def _error(code: str, message: str) -> dict[str, object]:
    return {'ok': False, 'error': {'code': code, 'message': message}}


def _parse(arguments: str) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(arguments or '{}')
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def investigate(
    session: Session,
    brief: Brief,
    conversation: context.Conversation,
    terminal: TerminalTool,
) -> LoopResult:
    """Call the model with tools until the terminal tool is accepted.

    Every way out is a `LoopResult`; nothing here raises on model or tool
    failure. When the step's tool calls are used up the model gets one more
    turn in which only the terminal tool and the ledger update run (the
    update uses no tool call), and, if that terminal call is refused or the
    turn only updated the ledger, one more terminal-only turn.

    Args:
        session: The run's shared state.
        brief: Node, role and turn cap for this step.
        conversation: History, appended to in place.
        terminal: Tool that ends the step, with its validator.

    Returns:
        Why and how the step ended.
    """
    tools = session.tools(terminal.tool, brief.tools)
    # Apply this after any role allowlist: no allowlist can grant memory
    # access to a tester or skeptic. Dispatch checks the resulting names.
    if brief.role not in ('agent', 'lead'):
        tools = tuple(
            tool for tool in tools if tool.name not in results.MEMORY_TOOLS
        )
    system = (
        prompts.system_prompt(session.config)
        if brief.system is None
        else brief.system
    )
    calls_made: list[str] = []
    invalid = 0
    turns = 0
    final_turn = False
    retried = False
    while True:
        reason = session.budget.stop_reason()
        if (
            reason is None
            and brief.max_turns is not None
            and turns >= brief.max_turns
        ):
            reason = 'model_turn_limit'
        if reason is not None:
            return LoopResult(reason, None, tuple(calls_made), invalid)
        request = contracts.ChatRequest(
            session.config.model_alias,
            system,
            session.config.max_output_tokens,
            tools,
            session.briefing,
            conversation.view(),
        )
        try:
            result = session.client.complete(
                request, run=session.run, case_id=session.task.case_id
            )
        except contracts.BudgetRefused as exc:
            _record_failure(session, brief, 'spend_refused', exc)
            return LoopResult('spend_refused', None, tuple(calls_made), invalid)
        except (
            contracts.ModelUnavailable,
            contracts.ProviderError,
            contracts.RetryDeferred,
            contracts.BillingUncertain,
        ) as exc:
            _record_failure(session, brief, 'model_error', exc)
            return LoopResult('model_error', None, tuple(calls_made), invalid)
        turns += 1
        session.budget.charge_turn(result.cost_usd)
        message = result.response.message
        conversation.append(message)
        _record_model_call(session, brief, request, result)
        if not message.tool_calls:
            conversation.append(
                contracts.Message('user', prompts.NO_TOOL_CALL_PROMPT)
            )
            continue
        accepted: dict[str, object] | None = None
        refused_before = invalid
        ledger_on_final = False
        for call in message.tool_calls:
            args = _parse(call.arguments)
            if accepted is not None:
                reply = _error('ignored', 'The step already ended.')
            elif call.name == terminal.tool.name:
                reply, accepted = _terminal(session, brief, terminal, args)
                invalid += int(accepted is None)
            elif call.name not in {tool.name for tool in tools}:
                reply = _error('unknown_tool', f'No tool named {call.name!r}')
            elif final_turn and call.name == ledger.UPDATE_TOOL_NAME:
                # Recording hypotheses is bookkeeping, not investigation:
                # on the last turn it runs without a tool call, so a run
                # that spent its calls on data still ends with a ledger.
                reply = _agent_tool(session, brief, call.name, args)
                ledger_on_final = True
            elif final_turn or not session.budget.charge_tool_call():
                reply = _error(
                    'tool_call_limit',
                    'The tool-call limit for this step is reached; call '
                    f'{terminal.tool.name}.',
                )
            elif call.name in session.executor.names():
                stored = session.executor.call(call.name, args)
                calls_made.append(stored.call_id)
                session.record.record(
                    'tool_call',
                    role=brief.role,
                    hypothesis_id=brief.hypothesis_id,
                    node=brief.node,
                    model_call_id=call.id,
                    **stored.to_dict(),
                )
                conversation.append(
                    contracts.Message(
                        'tool',
                        executor.result_text(stored.result),
                        tool_call_id=call.id,
                    ),
                    data_call_id=stored.call_id,
                )
                continue
            else:
                reply = _agent_tool(session, brief, call.name, args)
            conversation.append(
                contracts.Message(
                    'tool', executor.result_text(reply), tool_call_id=call.id
                )
            )
        if accepted is not None:
            return LoopResult('submitted', accepted, tuple(calls_made), invalid)
        if (
            final_turn
            and (invalid > refused_before or ledger_on_final)
            and not retried
        ):
            # The terminal call was refused on the last turn, or the turn
            # only updated the ledger: one more turn, still terminal-only.
            retried = True
            conversation.append(
                contracts.Message(
                    'user',
                    prompts.RETRY_TERMINAL_PROMPT.format(
                        terminal=terminal.tool.name
                    ),
                )
            )
            continue
        if final_turn:
            return LoopResult(
                'tool_call_limit', None, tuple(calls_made), invalid
            )
        if session.budget.tool_calls_left() <= 0:
            final_turn = True
            conversation.append(
                contracts.Message(
                    'user',
                    prompts.FINAL_CALL_PROMPT.format(
                        terminal=terminal.tool.name
                    ),
                )
            )


def _terminal(
    session: Session,
    brief: Brief,
    terminal: TerminalTool,
    args: dict[str, object] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    if args is None:
        parsed, errors = None, ['arguments must be a JSON object']
    else:
        ranks = args.get('ranks')
        if isinstance(ranks, list) and all(isinstance(r, str) for r in ranks):
            session.stated_ranks = tuple(str(rank) for rank in ranks)
        parsed, errors = terminal.validate(args)
    if parsed is None:
        session.record.record(
            'submit_rejected',
            role=brief.role,
            hypothesis_id=brief.hypothesis_id,
            node=brief.node,
            tool=terminal.tool.name,
            arguments=args,
            errors=errors,
        )
        return {'ok': False, 'errors': errors}, None
    return {'ok': True}, parsed


def _agent_tool(
    session: Session,
    brief: Brief,
    name: str,
    args: dict[str, object] | None,
) -> dict[str, object]:
    if args is None:
        return _error('invalid_args', 'arguments must be a JSON object')
    if name == ledger.UPDATE_TOOL_NAME and session.ledger is not None:
        result = session.ledger.apply(args)
        session.record.record(
            'ledger',
            role=brief.role,
            hypothesis_id=brief.hypothesis_id,
            node=brief.node,
            arguments=args,
            result=result,
            ledger=session.ledger.to_dict(),
        )
        return result
    stored = session.executor.store.get(str(args.get('call_id')))
    if (
        stored is not None
        and stored.from_memory
        and brief.role not in ('agent', 'lead')
    ):
        result = _error('forbidden', 'This role cannot read memory results')
    else:
        result = session.executor.read(args.get('call_id'))
    session.record.record(
        'agent_tool',
        role=brief.role,
        hypothesis_id=brief.hypothesis_id,
        node=brief.node,
        tool=name,
        arguments=args,
        ok=result.get('ok', True) is not False,
    )
    return result


def _record_model_call(
    session: Session,
    brief: Brief,
    request: contracts.ChatRequest,
    result: contracts.ChatResult,
) -> None:
    response = result.response
    session.record.record(
        'model_call',
        role=brief.role,
        hypothesis_id=brief.hypothesis_id,
        node=brief.node,
        requested_model=result.requested_model,
        served_model=response.served_model,
        request_id=response.request_id,
        reservation_id=result.reservation_id,
        started_at=result.started_at,
        history_messages=len(request.history),
        message=trajectory.message_to_dict(response.message),
        usage=dataclasses.asdict(response.usage),
        cost_usd=str(result.cost_usd),
        limit_usd=str(result.limit_usd),
        budget=session.budget.to_dict(),
    )


def _record_failure(
    session: Session, brief: Brief, reason: str, exc: Exception
) -> None:
    session.record.record(
        'model_error',
        role=brief.role,
        hypothesis_id=brief.hypothesis_id,
        node=brief.node,
        reason=reason,
        error=type(exc).__name__,
        message=str(exc),
        retry_after=(
            exc.retry_after
            if isinstance(exc, contracts.RetryDeferred)
            else None
        ),
    )


SUBMIT_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'outcome': {
            'type': 'string',
            'enum': ['diagnosis', 'no_fault_found'],
        },
        'ranks': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': 'Root-cause components, best first.',
        },
        'fault_type': {'type': 'string'},
        'confidence': {
            'type': 'string',
            'enum': list(report.CONFIDENCE_LEVELS),
        },
        'confidence_reasons': {'type': 'array', 'items': {'type': 'string'}},
        'actions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'hypothesis_id': {'type': 'string'},
                    'action': {'type': 'string'},
                    'risk': {'type': 'string', 'enum': list(report.RISK_ORDER)},
                    'how_to_verify': {'type': 'string'},
                },
                'required': [
                    'hypothesis_id',
                    'action',
                    'risk',
                    'how_to_verify',
                ],
            },
        },
        'data_requests': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'hypothesis_id': {'type': 'string'},
                    'what': {'type': 'string'},
                    'source': {'type': 'string'},
                    'effect': {'type': 'string'},
                },
                'required': ['hypothesis_id', 'what', 'source', 'effect'],
            },
        },
    },
    'required': ['outcome', 'ranks'],
}


def _alias_key(name: str) -> str:
    """Return a spelling-insensitive key: `frontendservice` ~ `frontend`."""
    flat = ''.join(ch for ch in name.casefold() if ch.isalnum())
    return flat.removesuffix('service') or flat


def _candidate_aliases(names: Iterable[str]) -> dict[str, str]:
    """Map alias keys to candidates, leaving out keys two candidates share."""
    seen: dict[str, list[str]] = {}
    for name in names:
        seen.setdefault(_alias_key(name), []).append(name)
    return {key: found[0] for key, found in seen.items() if len(found) == 1}


def submit_tool(session: Session) -> TerminalTool:
    """Return the `submit` tool and its validator for this run.

    With candidate lists (benchmark cases), every rank must come from them;
    without (uploads), ranks are free text. Actions and data requests are
    checked later, in finalisation, and dropped there if invalid.

    Args:
        session: The run, for its task and ledger.

    Returns:
        The terminal tool for the investigate step.
    """
    allowed = {
        name.casefold(): name
        for name in (
            *session.task.candidate_services,
            *session.task.candidate_indicators,
        )
    }

    def validate(
        args: dict[str, object],
    ) -> tuple[dict[str, object] | None, list[str]]:
        errors: list[str] = []
        outcome = args.get('outcome')
        ranks = args.get('ranks')
        if outcome not in ('diagnosis', 'no_fault_found'):
            errors.append('outcome must be "diagnosis" or "no_fault_found"')
        if not isinstance(ranks, list) or not all(
            isinstance(item, str) and item.strip() for item in ranks
        ):
            return None, [*errors, 'ranks must be a list of names']
        names = [str(item).strip() for item in ranks]
        if outcome == 'diagnosis' and not names:
            errors.append('a diagnosis needs at least one ranked component')
        if outcome == 'no_fault_found' and names:
            errors.append('no_fault_found takes an empty ranks list')
        if len({name.casefold() for name in names}) != len(names):
            errors.append('ranks must not repeat a name')
        if allowed:
            unknown = [name for name in names if name.casefold() not in allowed]
            aliases = _candidate_aliases(allowed.values())
            for name in list(unknown):
                match = aliases.get(_alias_key(name))
                if match is not None:
                    allowed[name.casefold()] = match
                    unknown.remove(name)
            if unknown:
                errors.append(
                    f'not in the candidate list: {unknown}; choose from '
                    f'{sorted(allowed.values())}'
                )
            names = [allowed.get(name.casefold(), name) for name in names]
        confidence = args.get('confidence')
        if confidence is not None and confidence not in (
            report.CONFIDENCE_LEVELS
        ):
            errors.append('confidence must be low, medium or high')
        for field in ('actions', 'data_requests', 'confidence_reasons'):
            if args.get(field) is not None and not isinstance(
                args.get(field), list
            ):
                errors.append(f'{field} must be a list')
        if (
            outcome == 'no_fault_found'
            and session.ledger is not None
            and any(
                entry.status == 'supported'
                for entry in session.ledger.entries.values()
            )
        ):
            errors.append(
                'a supported hypothesis exists; reject it before reporting '
                'no_fault_found'
            )
        if errors:
            return None, errors
        return {**args, 'ranks': names}, []

    return TerminalTool(
        contracts.Tool(
            SUBMIT_NAME,
            'End the investigation with a ranked diagnosis, or with '
            '"no_fault_found" when the data shows nothing abnormal.',
            executor.json_schema(SUBMIT_SCHEMA),
        ),
        validate,
    )
