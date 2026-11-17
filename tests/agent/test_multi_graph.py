"""The multi-agent graph with role-routed scripted models (Phase 07)."""

import threading
from collections.abc import Callable
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import continuation
from incidents_agent.agent import fake_policy
from incidents_agent.agent import graph
from incidents_agent.agent import prompts
from incidents_agent.agent import state
from incidents_agent.llm import contracts
from incidents_agent.memory import store as incident_memory
from tests.agent import scripted

_MULTI = config.AgentConfig(mode='multi')
_GATEWAY_SEARCH: dict[str, object] = {
    'table': 'service',
    'pattern': 'queue saturated',
}
_LOCK_QUOTE = 'inventory-db Lock wait timeout'
_GATEWAY_QUOTE = 'api-gateway Request queue saturated'


def _hypotheses() -> contracts.Message:
    return scripted.call(
        'update_hypotheses',
        operations=[
            {
                'op': 'add',
                'statement': 'inventory-db lock contention',
                'component': 'inventory-db',
                'evidence': [scripted.LOCK_EVIDENCE],
            },
            {
                'op': 'add',
                'statement': 'api-gateway queue saturation',
                'component': 'api-gateway',
            },
            {
                'op': 'add',
                'statement': 'order-service retry storm',
                'component': 'order-service',
            },
        ],
    )


def _lead_pass() -> list[scripted.Turn]:
    return [
        scripted.call('search_text', **scripted.LOCK_SEARCH),
        _hypotheses(),
        scripted.dispatch('H1', 'H2', 'H3'),
    ]


def _testers() -> dict[str, Sequence[scripted.Turn]]:
    return {
        'tester:H1': [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.verdict('H1', 'supported', _LOCK_QUOTE),
        ],
        'tester:H2': [
            scripted.call('search_text', **_GATEWAY_SEARCH),
            scripted.verdict(
                'H2', 'rejected', _GATEWAY_QUOTE, reason='follows the db'
            ),
        ],
        'tester:H3': [
            scripted.verdict('H3', 'undecided', reason='nothing to compare')
        ],
    }


def _skeptic(outcome: str = 'holds') -> list[scripted.Turn]:
    if outcome == 'disproved':
        return [
            scripted.call('search_text', **_GATEWAY_SEARCH),
            scripted.challenge('H1', outcome, _GATEWAY_QUOTE, 'api-gateway'),
        ]
    return [
        scripted.call('search_text', **scripted.LOCK_SEARCH),
        scripted.challenge('H1', outcome, _LOCK_QUOTE),
    ]


def _run(
    tmp_path: Path,
    client: contracts.ModelClient,
    *,
    agent_config: config.AgentConfig = _MULTI,
    clock: list[float] | None = None,
) -> graph.RunResult:
    now = clock if clock is not None else [0.0]
    return graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        agent_config,
        runs_dir=tmp_path,
        run_id='run-multi',
        clock=lambda: now[0],
    )


def _nodes(result: graph.RunResult) -> list[str]:
    return [
        str(event['node'])
        for event in result.events
        if event['type'] == 'node' and event['role'] != 'tester'
    ]


def _ledger(result: graph.RunResult) -> dict[str, dict[str, object]]:
    merged = [
        event
        for event in result.events
        if event['type'] == 'ledger' and event.get('node') == 'merge'
    ]
    book = merged[-1]['ledger']
    assert isinstance(book, dict)
    entries = book['entries']
    assert isinstance(entries, list)
    return {str(item['id']): item for item in entries}


def _happy_client(
    *,
    before: Callable[[str, int], None] | None = None,
    skeptic: str = 'holds',
) -> scripted.RoutedClient:
    return scripted.RoutedClient(
        {
            'lead': [
                *_lead_pass(),
                scripted.submit('inventory-db', confidence='medium'),
            ],
            **_testers(),
            'skeptic': _skeptic(skeptic),
        },
        before=before,
    )


def test_happy_path_with_three_testers_reports_verified_cause(
    tmp_path: Path,
) -> None:
    """Lead, three testers, skeptic and report produce a verified cause."""
    result = _run(tmp_path, _happy_client())

    assert _nodes(result) == [
        'orient',
        'lead',
        'merge',
        'skeptic',
        'lead_report',
        'report',
    ]
    assert result.report.outcome == 'verified'
    assert result.report.ranks[0] == 'inventory-db'
    assert {
        hid: (entry['status'], entry['tester_verdict'])
        for hid, entry in _ledger(result).items()
    } == {
        'H1': ('supported', 'supported'),
        'H2': ('rejected', 'rejected'),
        'H3': ('open', 'undecided'),
    }


def test_testers_run_in_parallel(tmp_path: Path) -> None:
    """All three testers are inside a model call at the same time."""
    gate = threading.Barrier(3, timeout=10)

    def before(key: str, index: int) -> None:
        if key.startswith('tester:') and index == 0:
            gate.wait()

    result = _run(tmp_path, _happy_client(before=before))

    assert result.report.outcome == 'verified'


def test_merge_does_not_depend_on_tester_finish_order(tmp_path: Path) -> None:
    """H1 finishing last gives the same ledger as H1 finishing first."""
    h3_done = threading.Event()

    def h1_last(key: str, index: int) -> None:
        if key == 'tester:H3':
            h3_done.set()
        if key == 'tester:H1' and index == 1:
            h3_done.wait(timeout=10)

    def h1_first(key: str, index: int) -> None:
        del key, index

    late = _run(tmp_path / 'late', _happy_client(before=h1_last))
    early = _run(tmp_path / 'early', _happy_client(before=h1_first))

    assert _ledger(late) == _ledger(early)
    assert late.report.ranks == early.report.ranks


def test_every_role_event_is_tagged_and_testers_name_their_hypothesis(
    tmp_path: Path,
) -> None:
    """Model and tool events carry a role; tester ones a hypothesis id."""
    result = _run(tmp_path, _happy_client())
    calls = [
        event
        for event in result.events
        if event['type'] in ('model_call', 'tool_call')
    ]

    assert {str(event['role']) for event in calls} == {
        'lead',
        'tester',
        'skeptic',
    }
    assert {
        event.get('hypothesis_id')
        for event in calls
        if event['role'] == 'tester'
    } == {'H1', 'H2', 'H3'}


def test_skeptic_sees_only_the_claim_and_its_evidence(tmp_path: Path) -> None:
    """The skeptic's history holds no message from the lead or testers."""
    client = _happy_client()

    _run(tmp_path, client)
    first = client.of('skeptic')[0]

    assert first.system_prompt == prompts.SKEPTIC_SYSTEM
    assert [message.role for message in first.history] == ['user']
    opening = str(first.history[0].text)
    assert opening.startswith('Claim H1:')
    assert 'follows the db' not in opening
    assert 'H2' not in opening


def test_skeptic_disproves_and_lead_corrects_exactly_once(
    tmp_path: Path,
) -> None:
    """A disproval sends the run back to the lead once, then it reports."""
    client = _happy_client(skeptic='disproved')

    result = _run(tmp_path, client)
    lead_last = client.of('lead')[-1]
    corrections = [
        message
        for message in lead_last.history
        if message.role == 'user'
        and str(message.text).startswith('The skeptic disproved')
    ]

    assert len(client.of('skeptic')) == 2
    assert len(corrections) == 1
    assert _nodes(result).count('lead_report') == 1
    assert result.report.outcome != 'verified'


def test_tester_at_its_limit_leaves_hypothesis_undecided(
    tmp_path: Path,
) -> None:
    """A tester out of tool calls is undecided with the reason recorded."""
    greedy = scripted.calls(*[('list_tables', {})] * 5)
    scripts = _testers()
    scripts['tester:H3'] = [greedy, scripted.call('list_tables')]
    client = scripted.RoutedClient(
        {
            'lead': [*_lead_pass(), scripted.submit('inventory-db')],
            **scripts,
            'skeptic': _skeptic(),
        }
    )

    result = _run(tmp_path, client)
    h3 = _ledger(result)['H3']

    assert (h3['status'], h3['tester_verdict']) == ('open', 'undecided')
    assert 'tool_call_limit' in str(h3['undecided_reason'])
    assert result.report.ranks[0] == 'inventory-db'


def test_tester_model_error_leaves_hypothesis_undecided_and_run_goes_on(
    tmp_path: Path,
) -> None:
    """A failing model call in one tester does not end the run."""
    scripts = _testers()
    scripts['tester:H2'] = [contracts.ProviderError('bad request')]
    client = scripted.RoutedClient(
        {
            'lead': [*_lead_pass(), scripted.submit('inventory-db')],
            **scripts,
            'skeptic': _skeptic(),
        }
    )

    result = _run(tmp_path, client)

    assert 'model_error' in str(_ledger(result)['H2']['undecided_reason'])
    assert result.report.outcome == 'verified'


def test_run_wall_time_reached_mid_fan_out_forces_report(
    tmp_path: Path,
) -> None:
    """The run's clock running out during the fan-out skips the skeptic."""
    now = [0.0]

    def tick(key: str, index: int) -> None:
        if key == 'tester:H1' and index == 0:
            now[0] = 601.0

    scripts = _testers()
    client = scripted.RoutedClient(
        {'lead': _lead_pass(), **scripts, 'skeptic': _skeptic()},
        before=tick,
    )

    result = _run(tmp_path, client, clock=now)

    assert 'skeptic' not in _nodes(result)
    assert result.report.stop_reason == 'wall_time'
    assert result.report.ranks[0] == 'inventory-db'


def test_total_tool_calls_never_exceed_the_single_agent_limit(
    tmp_path: Path,
) -> None:
    """Roles that each try to overspend stay within the run's limit."""
    greedy = scripted.calls(*[('list_tables', {})] * 10)
    client = scripted.RoutedClient(
        {
            'lead': [
                greedy,
                _hypotheses(),
                scripted.dispatch('H1', 'H2'),
                greedy,
                scripted.submit('inventory-db'),
            ],
            'tester:H1': [greedy, greedy],
            'tester:H2': [greedy, greedy],
            'skeptic': [greedy, greedy],
        }
    )

    result = _run(tmp_path, client)
    tool_calls = [e for e in result.events if e['type'] == 'tool_call']

    assert len(tool_calls) <= _MULTI.limits.tool_calls
    total = int(str(result.report.counters['tool_calls']))
    assert total <= _MULTI.limits.tool_calls


def test_lead_no_fault_found_skips_testers(tmp_path: Path) -> None:
    """The lead may end at once with no_fault_found."""
    client = scripted.RoutedClient(
        {'lead': [scripted.call('dispatch', outcome='no_fault_found')]}
    )

    result = _run(tmp_path, client)

    assert result.report.outcome == 'no_fault_found'
    assert _nodes(result) == ['orient', 'lead', 'report']


def test_lead_without_dispatch_falls_back_to_ledger_briefs(
    tmp_path: Path,
) -> None:
    """A lead that runs out before dispatching still gets its ideas tested."""
    stall = scripted.call('list_tables')
    client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                _hypotheses(),
                stall,
                stall,
                scripted.submit('inventory-db'),
            ],
            **_testers(),
            'skeptic': _skeptic(),
        }
    )

    result = _run(tmp_path, client)
    dispatched = [e for e in result.events if e['type'] == 'dispatch']

    assert dispatched[0]['fallback'] is True
    assert result.report.ranks[0] == 'inventory-db'


def test_only_the_lead_is_offered_past_incidents(tmp_path: Path) -> None:
    """Memory lookups are a lead tool; testers and skeptic never see it."""
    client = _happy_client()
    settings = config.AgentConfig(mode='multi', memory=True)

    graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        settings,
        runs_dir=tmp_path,
        memory_store=incident_memory.MemoryStore(tmp_path / 'memory.db'),
    )

    def names(key: str) -> set[str]:
        return {tool.name for tool in client.of(key)[0].tools}

    assert 'search_past_incidents' in names('lead')
    assert 'search_past_incidents' not in names('tester:H1')
    assert 'search_past_incidents' not in names('skeptic')
    assert 'update_hypotheses' not in names('tester:H1')


def test_skeptic_uses_its_own_model_alias(tmp_path: Path) -> None:
    """A separately configured skeptic model is the one it calls."""
    client = _happy_client()
    settings = config.AgentConfig(mode='multi', skeptic_model_alias='other')

    _run(tmp_path, client, agent_config=settings)

    assert client.of('skeptic')[0].model_alias == 'other'
    assert client.of('lead')[0].model_alias == ''


def test_manifest_and_report_record_multi_mode(tmp_path: Path) -> None:
    """The run says which architecture made it and what it spent in all."""
    result = _run(tmp_path, _happy_client())
    manifest = result.events[0]['manifest']

    assert isinstance(manifest, dict)
    assert manifest['mode'] == 'multi'
    assert set(manifest['role_prompt_hashes']) == {'lead', 'tester', 'skeptic'}
    assert result.report.counters['tool_calls'] == 5


def test_report_cites_a_tester_call(tmp_path: Path) -> None:
    """Evidence from any role's calls in the run grounds the report."""
    result = _run(tmp_path, _happy_client())
    tester_calls = {
        str(e['call_id'])
        for e in result.events
        if e['type'] == 'tool_call' and e['role'] == 'tester'
    }

    cited = {ref.call_id for ref in result.report.causes[0].evidence}

    assert cited & tester_calls


def test_rule_client_runs_multi_mode_end_to_end(tmp_path: Path) -> None:
    """The free dry-run model drives every role to a report."""
    result = _run(tmp_path, fake_policy.RuleClient())

    roles_seen = {str(e['role']) for e in result.events}

    assert {'lead', 'tester', 'skeptic'} <= roles_seen
    assert result.report.outcome in ('provisional', 'verified')
    assert result.report.counters['cost_usd'] == str(Decimal(0))


def test_continuation_of_a_multi_run_runs_the_multi_graph(
    tmp_path: Path,
) -> None:
    """Adding files to a multi run continues in multi mode."""
    first = _run(tmp_path, fake_policy.RuleClient())
    assert first.run_dir is not None
    extra = tmp_path / 'notes.md'
    extra.write_text('# Notes\n\nNothing new.\n', encoding='utf-8')

    result = continuation.continue_run(
        first.run_dir, [extra], fake_policy.RuleClient()
    )

    later = [e for e in result.events if e['continuation'] == 1]
    assert {'lead', 'tester'} <= {str(e['role']) for e in later}


@pytest.mark.parametrize(
    'missing',
    [
        pytest.param('tester:H1', id='tester'),
        pytest.param('skeptic', id='skeptic'),
    ],
)
def test_role_without_script_fails_loudly(tmp_path: Path, missing: str) -> None:
    """A routing mistake in a test surfaces instead of hanging."""
    scripts: dict[str, Sequence[scripted.Turn]] = {
        'lead': [*_lead_pass(), scripted.submit('inventory-db')],
        **_testers(),
        'skeptic': _skeptic(),
    }
    del scripts[missing]

    with pytest.raises(AssertionError, match='No scripted turn'):
        _run(tmp_path, scripted.RoutedClient(scripts))


def test_lead_that_spends_every_call_on_data_still_dispatches(
    tmp_path: Path,
) -> None:
    """Hypotheses arrive with dispatch, so a lead out of calls still briefs.

    This is the failure the live test found: the lead batched its three
    tool calls on data, and `update_hypotheses` was refused.
    """
    data = scripted.calls(
        ('search_text', scripted.LOCK_SEARCH),
        ('search_text', _GATEWAY_SEARCH),
        ('list_tables', {}),
    )
    briefs = scripted.call(
        'dispatch',
        outcome='dispatch',
        briefs=[
            {
                'component': 'inventory-db',
                'statement': 'lock contention',
                'evidence': [scripted.LOCK_EVIDENCE],
                'confirm_if': 'lock waits start first',
                'refute_if': 'lock waits follow the gateway',
            },
            {
                'component': 'api-gateway',
                'statement': 'queue saturation',
                'confirm_if': 'queue saturates first',
                'refute_if': 'queue saturates after db errors',
            },
        ],
    )
    scripts = _testers()
    del scripts['tester:H3']
    client = scripted.RoutedClient(
        {
            'lead': [data, briefs, scripted.submit('inventory-db')],
            **scripts,
            'skeptic': _skeptic(),
        }
    )

    result = _run(tmp_path, client)

    assert set(_ledger(result)) == {'H1', 'H2'}
    assert result.report.outcome == 'verified'
    assert result.report.ranks[0] == 'inventory-db'


def test_lead_briefing_one_component_twice_still_reaches_testers(
    tmp_path: Path,
) -> None:
    """Duplicate-component briefs are merged, so the fan-out happens."""
    briefs = scripted.call(
        'dispatch',
        outcome='dispatch',
        briefs=[
            {
                'component': 'inventory-db',
                'statement': 'lock contention',
                'evidence': [scripted.LOCK_EVIDENCE],
                'confirm_if': 'lock waits start first',
                'refute_if': 'lock waits follow the gateway',
            },
            {
                'component': 'inventory-db',
                'statement': 'pool exhaustion',
                'confirm_if': 'pool hits 100/100 first',
                'refute_if': 'pool is fine',
            },
        ],
    )
    client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                briefs,
                scripted.submit('inventory-db'),
            ],
            'tester:H1': _testers()['tester:H1'],
            'skeptic': _skeptic(),
        }
    )

    result = _run(tmp_path, client)

    assert set(_ledger(result)) == {'H1'}
    assert result.report.outcome == 'verified'
