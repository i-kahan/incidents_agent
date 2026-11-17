"""Builders for scripted model turns and agent sessions in tests."""

import collections
import itertools
import json
import re
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
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
from incidents_agent.agent import roles
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


# Multi mode (Phase 07). A turn may be a function of the request, so a
# tester can cite the call id it was just given, whatever order the
# parallel testers ran in.
type Turn = (
    contracts.Message
    | contracts.ChatResult
    | Exception
    | Callable[[contracts.ChatRequest], contracts.Message]
)

_TESTER_KEY = re.compile(r'Hypothesis (H\d+) ')
_ROLE_BY_SYSTEM = {
    prompts.LEAD_SYSTEM: 'lead',
    prompts.TESTER_SYSTEM: 'tester',
    prompts.SKEPTIC_SYSTEM: 'skeptic',
}


def route_key(request: contracts.ChatRequest) -> str:
    """Return `lead`, `tester:<id>` or `skeptic` for a multi-mode request."""
    role = _ROLE_BY_SYSTEM.get(request.system_prompt, 'agent')
    if role != 'tester':
        return role
    first = next(m.text or '' for m in request.history if m.role == 'user')
    match = _TESTER_KEY.search(first)
    return f'tester:{match.group(1) if match else "?"}'


class RoutedClient:
    """A scripted model client with one script per role (and tester).

    Safe under the multi graph's threads: each request is routed by its
    system prompt (and, for testers, the hypothesis in the brief), so the
    order in which testers run does not change which turn each one gets.
    """

    def __init__(
        self,
        scripts: Mapping[str, Sequence[Turn]],
        *,
        before: Callable[[str, int], None] | None = None,
    ) -> None:
        """Hold the scripts.

        Args:
            scripts: Turns by route key (`lead`, `tester:H1`, `skeptic`).
            before: Called with the key and the turn index before each
                turn, for tests that control timing.
        """
        self._scripts = {
            key: collections.deque(turns) for key, turns in scripts.items()
        }
        self._before = before
        self._lock = threading.Lock()
        self._counts: dict[str, int] = collections.defaultdict(int)
        self.requests: list[tuple[str, contracts.ChatRequest]] = []

    def complete(
        self,
        request: contracts.ChatRequest,
        *,
        run: contracts.Run,
        case_id: str | None = None,
    ) -> contracts.ChatResult:
        """Return the next scripted turn for the request's route."""
        del run, case_id
        key = route_key(request)
        with self._lock:
            index = self._counts[key]
            self._counts[key] += 1
            self.requests.append((key, request))
        if self._before is not None:
            self._before(key, index)
        with self._lock:
            script = self._scripts.get(key)
            if not script:
                raise AssertionError(f'No scripted turn left for {key}')
            turn = script.popleft()
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, contracts.ChatResult):
            return turn
        message = turn if isinstance(turn, contracts.Message) else turn(request)
        return costly(message, '0')

    def of(self, key: str) -> list[contracts.ChatRequest]:
        """Return the requests routed to `key`, in order."""
        with self._lock:
            return [request for name, request in self.requests if name == key]


def last_call_id(request: contracts.ChatRequest) -> str:
    """Return the data-tool call id in the latest tool result shown."""
    for message in reversed(request.history):
        if message.role == 'tool':
            body = json.loads(message.text or '{}')
            if isinstance(body, dict) and 'call_id' in body:
                return str(body['call_id'])
    raise AssertionError('No data-tool result in the history')


def verdict(
    hypothesis_id: str,
    kind: str,
    quote: str | None = None,
    **extra: object,
) -> Callable[[contracts.ChatRequest], contracts.Message]:
    """Return a turn recording a tester verdict, citing its last call."""

    def turn(request: contracts.ChatRequest) -> contracts.Message:
        evidence = (
            []
            if quote is None
            else [
                {
                    'call_id': last_call_id(request),
                    'locator': 'service',
                    'quote': quote,
                }
            ]
        )
        return call(
            roles.VERDICT_NAME,
            hypothesis_id=hypothesis_id,
            verdict=kind,
            evidence=evidence,
            **extra,
        )

    return turn


def challenge(
    hypothesis_id: str,
    outcome: str,
    quote: str | None = None,
    alternative: str | None = None,
) -> Callable[[contracts.ChatRequest], contracts.Message]:
    """Return a turn recording the skeptic's outcome, citing its last call."""

    def turn(request: contracts.ChatRequest) -> contracts.Message:
        evidence = (
            []
            if quote is None
            else [
                {
                    'call_id': last_call_id(request),
                    'locator': 'service',
                    'quote': quote,
                }
            ]
        )
        return call(
            roles.CHALLENGE_NAME,
            hypothesis_id=hypothesis_id,
            check='does it call an affected service that failed first?',
            outcome=outcome,
            evidence=evidence,
            **(
                {}
                if alternative is None
                else {'alternative_component': alternative}
            ),
        )

    return turn


def dispatch(*hypothesis_ids: str) -> contracts.Message:
    """Return a lead turn dispatching one brief per hypothesis."""
    return call(
        roles.DISPATCH_NAME,
        outcome='dispatch',
        briefs=[
            {
                'hypothesis_id': hid,
                'confirm_if': f'{hid} errors start first',
                'refute_if': f'{hid} errors follow another component',
            }
            for hid in hypothesis_ids
        ],
    )
