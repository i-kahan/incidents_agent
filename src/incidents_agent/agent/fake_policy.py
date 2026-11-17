"""A rule-driven stand-in model for `--fake` runs, at no cost.

A fixed script cannot quote values from a folder it has not seen, so this
client reads the tool results in the conversation and follows a short,
deterministic recipe: find the onset, compare windows there, record the
top-shifting column as a cited hypothesis, submit it, and answer the
verify step with an `inconclusive` check. In multi mode (Phase 07) it
recognises each role by its system prompt: the lead dispatches its one
hypothesis, a tester repeats the comparison and supports the hypothesis
when the top column names its component, and the skeptic answers with an
`inconclusive` check. It exercises the whole graph, the citation check and
the run record; it is not meant to be right.
"""

import json
import re
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from decimal import Decimal

from incidents_agent.agent import graph
from incidents_agent.agent import investigate
from incidents_agent.agent import ledger
from incidents_agent.agent import prompts
from incidents_agent.agent import roles
from incidents_agent.llm import contracts

_VERIFY_MARKER = 'try to disprove it'
_TARGET = re.compile(r'Top hypothesis: (\S+) \(')
_TESTER = re.compile(r'Hypothesis (\S+) \((.+?)\):')
_CLAIM = re.compile(r'Claim (\S+):')
_METRIC_SUFFIXES = frozenset(
    {
        'cpu',
        'mem',
        'memory',
        'latency',
        'ms',
        'queue',
        'depth',
        'error',
        'errors',
        'rate',
        'count',
        'diskio',
        'net',
        'socket',
        'workingset',
        'qps',
        'requests',
        'duration',
        'active',
        'connections',
        'lock',
        'wait',
        'p50',
        'p90',
        'p95',
        'p99',
        's',
    }
)


def component_from_column(column: str, candidates: tuple[str, ...]) -> str:
    """Guess the component a metric column belongs to.

    Args:
        column: Column name such as `checkoutservice_cpu`.
        candidates: Candidate names, matched first when given.

    Returns:
        A candidate contained in the column, else the column without its
        metric suffixes, with underscores as hyphens.
    """
    flat = column.casefold().replace('-', '_')
    for name in sorted(candidates, key=len, reverse=True):
        if name.casefold().replace('-', '_') in flat:
            return name
    tokens = column.split('_')
    while len(tokens) > 1 and tokens[-1].casefold() in _METRIC_SUFFIXES:
        tokens.pop()
    return '-'.join(tokens)


class RuleClient:
    """A deterministic `ModelClient` that follows a fixed recipe."""

    def __init__(self, candidates: tuple[str, ...] = ()) -> None:
        """Remember the candidate names, used to name components."""
        self._candidates = candidates

    def complete(
        self,
        request: contracts.ChatRequest,
        *,
        run: contracts.Run,
        case_id: str | None = None,
    ) -> contracts.ChatResult:
        """Return the next step of the recipe for this conversation."""
        del case_id
        message = self._next(request)
        call_id = f'rule-{len(request.history)}'
        return contracts.ChatResult(
            contracts.ProviderResult(
                message, contracts.Usage(0, 0), 'rule-fake', call_id
            ),
            call_id,
            datetime(1970, 1, 1, tzinfo=UTC).isoformat(),
            'rule-fake',
            Decimal(0),
            Decimal(0),
        )

    def _next(self, request: contracts.ChatRequest) -> contracts.Message:
        tools = {tool.name for tool in request.tools}
        done = _tool_results(request.history)
        if request.system_prompt == prompts.TESTER_SYSTEM:
            return self._tester(request, done)
        if request.system_prompt == prompts.SKEPTIC_SYSTEM:
            return self._skeptic(request, done)
        user_texts = [m.text or '' for m in request.history if m.role == 'user']
        verifying = any(_VERIFY_MARKER in text for text in user_texts)
        step = len(request.history)
        if verifying:
            return self._verify(user_texts, done, step)
        onset = done.get('find_onset')
        if onset is None:
            return _call(step, 'find_onset', {})
        compare = done.get('compare_windows')
        signals = onset[1].get('signals')
        if compare is None and isinstance(signals, list) and signals:
            top = signals[0] if isinstance(signals[0], dict) else {}
            return _call(
                step,
                'compare_windows',
                {
                    'table': top.get('table'),
                    'split_time': onset[1].get('estimated_onset_time_s'),
                },
            )
        dispatching = roles.DISPATCH_NAME in tools
        if compare is None or not compare[1].get('ok'):
            if dispatching:
                return _call(
                    step, roles.DISPATCH_NAME, {'outcome': 'no_fault_found'}
                )
            return _call(
                step,
                investigate.SUBMIT_NAME,
                {'outcome': 'no_fault_found', 'ranks': []},
            )
        ranked = compare[1].get('ranked')
        first = ranked[0] if isinstance(ranked, list) and ranked else {}
        column = str(first.get('column', '')) if isinstance(first, dict) else ''
        component = component_from_column(column, self._candidates)
        if ledger.UPDATE_TOOL_NAME in tools and 'update_hypotheses' not in done:
            return _call(
                step,
                ledger.UPDATE_TOOL_NAME,
                {
                    'operations': [
                        {
                            'op': 'add',
                            'statement': f'{column} shifted most at the onset',
                            'component': component,
                            'confidence': 'low',
                            'evidence': [
                                {
                                    'call_id': compare[0],
                                    'locator': str(compare[1].get('table')),
                                    'quote': column,
                                }
                            ],
                        }
                    ]
                },
            )
        if dispatching:
            return _call(
                step,
                roles.DISPATCH_NAME,
                {
                    'outcome': 'dispatch',
                    'briefs': [
                        {
                            'hypothesis_id': 'H1',
                            'confirm_if': f'{column} shifts at the onset',
                            'refute_if': f'{column} is flat at the onset',
                        }
                    ],
                },
            )
        return _call(
            step,
            investigate.SUBMIT_NAME,
            {
                'outcome': 'diagnosis',
                'ranks': [component],
                'confidence': 'low',
                'confidence_reasons': ['rule-based fake model'],
                'actions': [
                    {
                        'hypothesis_id': 'H1'
                        if ledger.UPDATE_TOOL_NAME in tools
                        else component,
                        'action': f'Inspect {component} around the onset',
                        'risk': 'read_only',
                        'how_to_verify': f'{column} returns to baseline',
                    }
                ],
            },
        )

    def _first_user(self, request: contracts.ChatRequest) -> str:
        users = [m.text or '' for m in request.history if m.role == 'user']
        return users[0] if users else ''

    def _tester(
        self,
        request: contracts.ChatRequest,
        done: Mapping[str, tuple[str, dict[str, object]]],
    ) -> contracts.Message:
        step = len(request.history)
        match = _TESTER.search(self._first_user(request))
        hid, component = match.groups() if match else ('', '')
        onset = done.get('find_onset')
        if onset is None:
            return _call(step, 'find_onset', {})
        signals = onset[1].get('signals')
        top = (
            signals[0]
            if isinstance(signals, list)
            and signals
            and isinstance(signals[0], dict)
            else {}
        )
        compare = done.get('compare_windows')
        if compare is None and top:
            return _call(
                step,
                'compare_windows',
                {
                    'table': top.get('table'),
                    'split_time': onset[1].get('estimated_onset_time_s'),
                },
            )
        ranked = None if compare is None else compare[1].get('ranked')
        first = ranked[0] if isinstance(ranked, list) and ranked else {}
        column = str(first.get('column', '')) if isinstance(first, dict) else ''
        if (
            compare is not None
            and column
            and (component_from_column(column, self._candidates) == component)
        ):
            return _call(
                step,
                roles.VERDICT_NAME,
                {
                    'hypothesis_id': hid,
                    'verdict': 'supported',
                    'confidence': 'low',
                    'evidence': [
                        {
                            'call_id': compare[0],
                            'locator': str(compare[1].get('table')),
                            'quote': column,
                        }
                    ],
                },
            )
        return _call(
            step,
            roles.VERDICT_NAME,
            {
                'hypothesis_id': hid,
                'verdict': 'undecided',
                'reason': 'rule-based fake model found no shift to cite',
            },
        )

    def _skeptic(
        self,
        request: contracts.ChatRequest,
        done: Mapping[str, tuple[str, dict[str, object]]],
    ) -> contracts.Message:
        step = len(request.history)
        match = _CLAIM.search(self._first_user(request))
        if 'dependency_graph' not in done:
            return _call(step, 'dependency_graph', {})
        return _call(
            step,
            roles.CHALLENGE_NAME,
            {
                'hypothesis_id': match.group(1) if match else '',
                'check': 'direction against the dependency graph '
                '(rule-based fake model; not judged)',
                'outcome': 'holds',
                'evidence': [],
            },
        )

    def _verify(
        self,
        user_texts: list[str],
        done: Mapping[str, tuple[str, dict[str, object]]],
        step: int,
    ) -> contracts.Message:
        verify_prompt = [text for text in user_texts if _VERIFY_MARKER in text][
            -1
        ]
        match = _TARGET.search(verify_prompt)
        target = match.group(1) if match else ''
        if 'dependency_graph' not in done:
            return _call(step, 'dependency_graph', {})
        return _call(
            step,
            graph.VERIFY_NAME,
            {
                'hypothesis_id': target,
                'check': 'direction against the dependency graph '
                '(rule-based fake model; not judged)',
                'outcome': 'inconclusive',
                'evidence': [],
            },
        )


def _call(step: int, name: str, args: dict[str, object]) -> contracts.Message:
    return contracts.Message(
        'assistant',
        None,
        (contracts.ToolCall(f'rule-{step}-{name}', name, json.dumps(args)),),
    )


def _tool_results(
    history: tuple[contracts.Message, ...],
) -> dict[str, tuple[str, dict[str, object]]]:
    """Return the latest result of each tool: (call id, result)."""
    names: dict[str, str] = {}
    for message in history:
        for call in message.tool_calls:
            names[call.id] = call.name
    latest: dict[str, tuple[str, dict[str, object]]] = {}
    for message in history:
        if message.role != 'tool' or message.tool_call_id not in names:
            continue
        try:
            body = json.loads(message.text or '')
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            continue
        name = names[message.tool_call_id]
        latest[name] = (str(body.get('call_id', '')), body)
    return latest
