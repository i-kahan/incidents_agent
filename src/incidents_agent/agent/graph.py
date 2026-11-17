"""The agent graph: orient, investigate, verify, report.

The graph is a LangGraph state graph whose nodes act on one `run_state.RunState`
held in Python. Tools, the ledger and finalisation are plain Python and do
not depend on LangGraph. State is checkpointed to JSON at every node exit.
"""

import dataclasses
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Protocol
from typing import TypedDict

from langgraph.graph import END
from langgraph.graph import START
from langgraph.graph import StateGraph

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.agent import config
from incidents_agent.agent import context
from incidents_agent.agent import executor
from incidents_agent.agent import finalise
from incidents_agent.agent import investigate
from incidents_agent.agent import ledger
from incidents_agent.agent import memory_record
from incidents_agent.agent import prompts
from incidents_agent.agent import render
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import run_state
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.llm import contracts
from incidents_agent.memory import store as incident_memory

VERIFY_NAME = 'record_verification'
_MAX_VERIFY_RUNS = 2
_VERIFY_OUTCOMES: tuple[report.Verification, ...] = (
    'supported',
    'refuted',
    'inconclusive',
)

VERIFY_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'hypothesis_id': {'type': 'string'},
        'check': {
            'type': 'string',
            'description': 'What you checked and why it could disprove it.',
        },
        'outcome': {
            'type': 'string',
            'enum': ['supported', 'refuted', 'inconclusive'],
        },
        'evidence': ledger.EVIDENCE_SCHEMA,
    },
    'required': ['hypothesis_id', 'check', 'outcome'],
}


@dataclasses.dataclass(frozen=True)
class RunResult:
    """What a finished run returns to its caller (CLI, runner or app).

    Attributes:
        run_id: Run identifier.
        run_dir: Folder holding the run record, if written.
        report: The final report.
        events: Every trajectory event, in order.
    """

    run_id: str
    run_dir: Path | None
    report: report.Report
    events: tuple[dict[str, object], ...]


class _GraphState(TypedDict):
    node: str


class _Step(Protocol):
    def __call__(self, state: _GraphState) -> _GraphState: ...


def _node(
    name: str,
    run: run_state.RunState,
    action: Callable[[run_state.RunState], None],
) -> _Step:
    def step(state: _GraphState) -> _GraphState:
        del state  # The graph state only carries the node name.
        run.session.record.record('node', node=name, phase='enter')
        action(run)
        run.checkpoint(name)
        return {'node': name}

    return step


def _orient(run: run_state.RunState) -> None:
    run.conversation.append(contracts.Message('user', run.opening))
    if not run.budget.reservation_ok():
        run.stop_reason = 'reservation_refused'


def _investigate(run: run_state.RunState) -> None:
    session = run.session
    brief = investigate.Brief()
    if run.pending_correction is not None:
        run.corrected = True
        turns = session.config.limits.correction_model_turns
        run.conversation.append(
            contracts.Message(
                'user',
                prompts.CORRECTION_PROMPT.format(
                    **run.pending_correction,
                    turns=turns,
                    tool_calls=session.budget.tool_calls_left(),
                ),
            )
        )
        run.pending_correction = None
        brief = investigate.Brief(max_turns=turns)
    outcome = investigate.investigate(
        session, brief, run.conversation, investigate.submit_tool(session)
    )
    if outcome.accepted is not None:
        run.submission = outcome.accepted
    run.stop_reason = outcome.stop_reason


def _already_checked(run: run_state.RunState, target: tuple[str, str]) -> bool:
    book = run.session.ledger
    if book is None:
        return target[1].casefold() in run.verdicts
    return book.entries[target[0]].verification != 'unverified'


def _route_after_investigate(run: run_state.RunState) -> str:
    session = run.session
    if run.stop_reason in state.HARD_STOPS or not session.config.verify:
        return 'report'
    if (
        run.submission is not None
        and run.submission.get('outcome') == 'no_fault_found'
    ):
        return 'report'
    target = run_state.target(run)
    if (
        target is None
        or run.verify_runs >= _MAX_VERIFY_RUNS
        or _already_checked(run, target)
    ):
        return 'report'
    if run.verify_runs and session.budget.tool_calls_left() <= 0:
        return 'report'
    return 'verify'


def _verify_tool(
    run: run_state.RunState, target: tuple[str, str], first_fresh: int
) -> investigate.TerminalTool:
    session = run.session
    hypothesis_id, component = target

    def fresh_calls() -> set[str]:
        return {
            item.call_id
            for item in session.executor.store
            if item.continuation == run.continuation
            and int(item.call_id.rsplit('_', 1)[1]) >= first_fresh
        }

    def validate(
        args: dict[str, object],
    ) -> tuple[dict[str, object] | None, list[str]]:
        outcome = report.pick(args.get('outcome'), _VERIFY_OUTCOMES)
        check = str(args.get('check') or '').strip()
        errors = []
        if args.get('hypothesis_id') != hypothesis_id:
            errors.append(f'hypothesis_id must be {hypothesis_id!r}')
        if outcome is None:
            errors.append('outcome must be supported, refuted or inconclusive')
        if not check:
            errors.append('check must say what was checked')
        if errors or outcome is None:
            return None, errors
        if session.ledger is not None:
            errors = session.ledger.record_verification(
                hypothesis_id,
                outcome,
                check,
                args.get('evidence'),
                fresh_calls(),
            )
        elif outcome != 'inconclusive':
            errors = _plain_verdict_errors(session, args, fresh_calls())
        if errors:
            return None, errors
        run.verdicts[component.casefold()] = finalise.Verdict(outcome, check)
        return {**args, 'outcome': outcome, 'check': check}, []

    return investigate.TerminalTool(
        contracts.Tool(
            VERIFY_NAME,
            'Record the outcome of the check that could disprove the top '
            'hypothesis, with evidence from the calls made for it.',
            executor.json_schema(VERIFY_SCHEMA),
        ),
        validate,
    )


def _plain_verdict_errors(
    session: investigate.Session, args: dict[str, object], fresh: set[str]
) -> list[str]:
    evidence = args.get('evidence')
    errors: list[str] = []
    valid = []
    for item in evidence if isinstance(evidence, list) else []:
        ref, error = ledger.check_citation(
            item, session.executor.store, session.run_id
        )
        if ref is None:
            errors.append(f'evidence rejected: {error}')
        else:
            valid.append(ref)
    if not any(
        ref.call_id in fresh
        and (stored := session.executor.store.get(ref.call_id)) is not None
        and not stored.from_memory
        for ref in valid
    ):
        errors.append('the outcome must cite a tool call made for this check')
        return errors
    return []


def _verify(run: run_state.RunState) -> None:
    session = run.session
    target = run_state.target(run)
    if target is None:
        return
    run.budget.release_reserve()
    run.verify_runs += 1
    hypothesis_id, component = target
    direction = (
        prompts.DIRECTION_HINT.format(component=component)
        if run.catalog_has_dependencies
        else ''
    )
    run.conversation.append(
        contracts.Message(
            'user',
            prompts.VERIFY_PROMPT.format(
                target=hypothesis_id,
                component=component,
                tool_calls=session.budget.tool_calls_left(),
                direction=direction,
            ),
        )
    )
    terminal = _verify_tool(
        run, target, session.executor.store.next_call_number()
    )
    outcome = investigate.investigate(
        session, investigate.Brief(node='verify'), run.conversation, terminal
    )
    accepted = outcome.accepted
    session.record.record(
        'verification',
        node='verify',
        hypothesis_id=hypothesis_id,
        component=component,
        outcome='unverified' if accepted is None else accepted['outcome'],
        check=None if accepted is None else accepted['check'],
        stop_reason=outcome.stop_reason,
    )
    if outcome.stop_reason in state.HARD_STOPS:
        run.stop_reason = outcome.stop_reason
    if (
        accepted is not None
        and accepted['outcome'] == 'refuted'
        and not run.corrected
    ):
        run.pending_correction = {
            'target': hypothesis_id,
            'component': component,
            'check': str(accepted['check']),
        }


def _route_after_verify(run: run_state.RunState) -> str:
    if run.pending_correction is not None:
        return 'investigate'
    return 'report'


def _report(run: run_state.RunState) -> None:
    session = run.session
    run.final = finalise.finalise(
        session,
        submission=run.submission,
        stop_reason=run.stop_reason,
        verdicts=run.verdicts,
        continuation=run.continuation,
    )
    session.record.record('final_report', report=run.final.to_dict())
    if run.run_dir is None:
        return
    markdown = render.render(
        run.final, render.call_tools(session.record.events)
    )
    suffixes = [''] + ([f'-c{run.continuation}'] if run.continuation else [])
    for suffix in suffixes:
        run_state.write_json(
            run.run_dir / f'report{suffix}.json', run.final.to_dict()
        )
        (run.run_dir / f'report{suffix}.md').write_text(
            markdown, encoding='utf-8'
        )


def build_graph(
    run: run_state.RunState,
) -> Callable[[], None]:
    """Compile the four-node graph over one run and return its runner.

    Args:
        run: The run the nodes act on.

    Returns:
        A function that runs the graph from `orient` to `report`.
    """
    builder = StateGraph(_GraphState)
    builder.add_node('orient', _node('orient', run, _orient))
    builder.add_node('investigate', _node('investigate', run, _investigate))
    builder.add_node('verify', _node('verify', run, _verify))
    builder.add_node('report', _node('report', run, _report))
    builder.add_edge(START, 'orient')
    builder.add_conditional_edges(
        'orient',
        lambda _: (
            'report'
            if run.stop_reason == 'reservation_refused'
            else 'investigate'
        ),
        ['investigate', 'report'],
    )
    builder.add_conditional_edges(
        'investigate',
        lambda _: _route_after_investigate(run),
        ['verify', 'report'],
    )
    builder.add_conditional_edges(
        'verify', lambda _: _route_after_verify(run), ['investigate', 'report']
    )
    builder.add_edge('report', END)
    graph = builder.compile()

    def invoke() -> None:
        graph.invoke({'node': 'start'})

    return invoke


def new_run_id(task: state.Task) -> str:
    """Return a fresh run id that names the case when there is one."""
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S')
    return f'{task.case_id or "upload"}-{stamp}-{uuid.uuid4().hex[:6]}'


def resolve_memory_store(
    agent_config: config.AgentConfig,
    *,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: Path | None = None,
    runs_dir: Path | None = None,
) -> incident_memory.MemoryStore | None:
    """Return the active memory store when enabled, else None.

    Args:
        agent_config: Agent configuration with `memory` and `memory_frozen`.
        memory_store: Optional caller-supplied store instance.
        memory_path: Optional explicit SQLite path when opening a store.
        runs_dir: Optional parent runs directory used for default storage.

    Returns:
        Configured `MemoryStore`, or None when `agent_config.memory` is False.
    """
    if not agent_config.memory:
        return None
    if memory_store is not None:
        if agent_config.memory_frozen and not memory_store.frozen:
            return incident_memory.MemoryStore(memory_store.path, frozen=True)
        return memory_store
    default_path = (
        runs_dir / 'memory.sqlite3'
        if runs_dir is not None
        else incident_memory.DEFAULT_PATH
    )
    return incident_memory.MemoryStore(
        memory_path or default_path,
        frozen=agent_config.memory_frozen,
    )


def build_session(
    task: state.Task,
    client: contracts.ModelClient,
    agent_config: config.AgentConfig,
    ingested: ingest.IngestResult,
    run: contracts.Run,
    record: trajectory.Trajectory,
    *,
    store: results.ResultStore,
    book: ledger.HypothesisLedger | None,
    continuation: int,
    clock: Callable[[], float],
    memory_store: incident_memory.MemoryStore | None = None,
) -> investigate.Session:
    """Build a session over an ingested folder with a fresh budget.

    Call ids continue from the store, so a continued run never reuses one.

    Args:
        task: The task.
        client: Model client.
        agent_config: Agent configuration.
        ingested: The ingested folder.
        run: Spend-guard run identity.
        record: Trajectory.
        store: Result store, empty or carried over.
        book: Ledger over `store`, or None when ablated.
        continuation: Continuation index.
        clock: Monotonic clock.
        memory_store: Optional incident memory store for lookup tool wiring.

    Returns:
        The session, with its briefing built.
    """
    registry = tools.create_default_registry(
        ingested,
        first_call_number=store.next_call_number(),
        memory_store=memory_store,
        run_id=run.id,
    )
    return investigate.Session(
        run=run,
        task=task,
        config=agent_config,
        client=client,
        executor=executor.ToolExecutor(registry, store, run.id, continuation),
        ledger=book,
        budget=state.Budget(
            agent_config.limits, reserve=agent_config.verify, clock=clock
        ),
        record=record,
        briefing=prompts.briefing(task, ingested.catalog, agent_config),
    )


def catalog_summary(catalog: ingest_schema.Catalog) -> dict[str, object]:
    """Return the table and document inventory kept in checkpoints."""
    return {
        'tables': {
            table.name: {
                'source_file': table.source_file,
                'row_count': table.row_count,
                'columns': len(table.columns),
            }
            for table in catalog.tables
        },
        'documents': sorted(doc.doc_id for doc in catalog.documents),
    }


def manifest(
    session: investigate.Session,
    *,
    mode: str = 'single',
    memory_snapshot_hash: str | None = None,
) -> dict[str, object]:
    """Return the run manifest: what produced this run."""
    data: dict[str, object] = {
        'run_id': session.run_id,
        'reserve_name': session.run.reserve_name,
        'mode': mode,
        'task': session.task.to_dict(),
        'config': session.config.to_dict(),
        'prompt_version': prompts.PROMPT_VERSION,
        'prompt_hash': prompts.prompt_hash(),
        'trajectory_version': trajectory.TRAJECTORY_VERSION,
        'created_at': datetime.now(UTC).isoformat(),
    }
    if memory_snapshot_hash is not None:
        data['memory_snapshot_hash'] = memory_snapshot_hash
    return data


def run_investigation(
    task: state.Task,
    client: contracts.ModelClient,
    agent_config: config.AgentConfig,
    *,
    runs_dir: Path | None = None,
    run_id: str | None = None,
    reserve_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: Path | None = None,
    ingest_config: ingest_schema.IngestConfig | None = None,
) -> RunResult:
    """Run the agent on one task, end to end.

    This is the run function Phases 06, 08 and 09 call. It ingests the
    task's folder, runs the graph, and writes `manifest.json`,
    `trajectory.jsonl`, `checkpoint.json` and `report.json` under
    `runs_dir/<run_id>/` when `runs_dir` is given.

    Args:
        task: What to investigate.
        client: Guarded model client, or a fake.
        agent_config: Agent configuration.
        runs_dir: Parent folder for run records; None keeps them in memory.
        run_id: Run id; a fresh one when None.
        reserve_name: Named spend reserve to draw on (the final run).
        clock: Monotonic clock, injectable for tests.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional path to the incident memory SQLite file.
        ingest_config: Optional ingest settings, such as per-file
            classification overrides chosen by a user.

    Returns:
        The run id, its folder, its report and its events.

    Raises:
        ValueError: The configuration asks for a mode not built yet.
    """
    if agent_config.mode != 'single':
        # TODO: planning/07-multi-agent.md Workstream B - the multi graph
        # replaces this refusal.
        raise ValueError(f'Agent mode {agent_config.mode!r} is not built yet')
    identifier = run_id or new_run_id(task)
    run_dir = None if runs_dir is None else runs_dir / identifier
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=False)
    active_memory = resolve_memory_store(
        agent_config,
        memory_store=memory_store,
        memory_path=memory_path,
        runs_dir=runs_dir,
    )
    workdir = (
        run_dir / 'db'
        if run_dir is not None
        else Path(tempfile.mkdtemp(prefix='incidents-run-'))
    )
    ingested = ingest.ingest_folder(
        task.folder, workdir=workdir, config=ingest_config
    )
    try:
        record = trajectory.Trajectory(
            None if run_dir is None else run_dir / 'trajectory.jsonl',
            identifier,
        )
        store = results.ResultStore()
        session = build_session(
            task,
            client,
            agent_config,
            ingested,
            contracts.Run(identifier, reserve_name),
            record,
            store=store,
            book=(
                ledger.HypothesisLedger(identifier, store)
                if agent_config.ledger
                else None
            ),
            continuation=0,
            clock=clock,
            memory_store=active_memory,
        )
        snapshot = (
            None if active_memory is None else active_memory.snapshot_hash()
        )
        details = manifest(
            session, mode=agent_config.mode, memory_snapshot_hash=snapshot
        )
        if run_dir is not None:
            run_state.write_json(run_dir / 'manifest.json', details)
        record.record('run_start', manifest=details)
        run = run_state.RunState(
            session,
            context.Conversation(agent_config.context, store),
            prompts.has_dependency_data(ingested.catalog),
            run_dir,
            catalog_summary(ingested.catalog),
        )
        build_graph(run)()
    finally:
        ingested.close()
    if run.final is None:
        raise RuntimeError('The graph ended without reaching report')
    if (
        active_memory is not None
        and not active_memory.frozen
        and run.final.causes
    ):
        active_memory.save(
            memory_record.build_record_from_run(
                task, run.catalog, run.final, store
            )
        )
    return RunResult(identifier, run_dir, run.final, tuple(record.events))
