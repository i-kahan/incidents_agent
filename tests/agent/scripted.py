"""Builders for scripted model turns and agent sessions in tests."""

import itertools
import json
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.agent import config
from incidents_agent.agent import context
from incidents_agent.agent import executor
from incidents_agent.agent import investigate
from incidents_agent.agent import ledger
from incidents_agent.agent import prompts
from incidents_agent.agent import results
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.llm import contracts

REPO = Path(__file__).resolve().parents[2]
FIELD_CASE = REPO / 'evalsets' / 'field' / 'cases' / 'victim_vs_cause'
RUN_ID = 'run-test'

_IDS = itertools.count(1)

# Observations that exist in the victim_vs_cause fixture.
LOCK_SEARCH: dict[str, object] = {'table': 'service', 'pattern': 'Lock wait'}
LOCK_EVIDENCE: dict[str, object] = {
    'call_id': 'call_0001',
    'locator': 'service',
    'quote': 'inventory-db Lock wait timeout',
}


def call(name: str, **args: object) -> contracts.Message:
    """Return an assistant turn that calls one tool."""
    return calls((name, args))


def calls(*items: tuple[str, dict[str, object]]) -> contracts.Message:
    """Return an assistant turn that calls several tools."""
    return contracts.Message(
        'assistant',
        None,
        tuple(
            contracts.ToolCall(f'tc-{next(_IDS)}', name, json.dumps(args))
            for name, args in items
        ),
    )


def add_hypothesis(
    component: str = 'inventory-db',
    evidence: list[dict[str, object]] | None = None,
    **extra: object,
) -> contracts.Message:
    """Return a turn adding one hypothesis to the ledger."""
    return call(
        ledger.UPDATE_TOOL_NAME,
        operations=[
            {
                'op': 'add',
                'statement': f'{component} lock contention',
                'component': component,
                'evidence': evidence if evidence is not None else [],
                **extra,
            }
        ],
    )


def submit(*ranks: str, **extra: object) -> contracts.Message:
    """Return a diagnosis submit turn."""
    return call(
        investigate.SUBMIT_NAME,
        outcome=extra.pop('outcome', 'diagnosis'),
        ranks=list(ranks),
        **extra,
    )


def costly(message: contracts.Message, cost: str) -> contracts.ChatResult:
    """Wrap a turn in a result that settled at `cost` dollars."""
    return contracts.ChatResult(
        contracts.ProviderResult(
            message, contracts.Usage(10, 5), 'fake-model', 'req'
        ),
        'reservation',
        datetime(1970, 1, 1, tzinfo=UTC).isoformat(),
        'fake-model',
        Decimal(cost),
        Decimal(10),
    )


def make_session(
    ingested: ingest.IngestResult,
    client: contracts.ModelClient,
    workdir: Path,
    *,
    agent_config: config.AgentConfig | None = None,
    task: state.Task | None = None,
) -> investigate.Session:
    """Build a session over an ingested folder, recording to `workdir`."""
    settings = agent_config or config.AgentConfig()
    store = results.ResultStore()
    the_task = task or state.Task(FIELD_CASE)
    session = investigate.Session(
        run=contracts.Run(RUN_ID),
        task=the_task,
        config=settings,
        client=client,
        executor=executor.ToolExecutor(
            tools.create_default_registry(ingested), store, RUN_ID
        ),
        ledger=(
            ledger.HypothesisLedger(RUN_ID, store) if settings.ledger else None
        ),
        budget=state.Budget(settings.limits, reserve=settings.verify),
        record=trajectory.Trajectory(workdir / 'trajectory.jsonl', RUN_ID),
    )
    session.briefing = prompts.briefing(the_task, ingested.catalog, settings)
    return session


def conversation(session: investigate.Session) -> context.Conversation:
    """Return an empty conversation using the session's context policy."""
    return context.Conversation(session.config.context, session.executor.store)
