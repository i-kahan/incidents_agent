"""Lead-only memory access, grounding and persistence across multi mode."""

import json
from pathlib import Path

import pytest

from incidents_agent import tools
from incidents_agent.agent import config
from incidents_agent.agent import continuation
from incidents_agent.agent import executor
from incidents_agent.agent import graph
from incidents_agent.agent import investigate
from incidents_agent.agent import ledger
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import roles
from incidents_agent.agent import state
from incidents_agent.llm import fake
from incidents_agent.memory import store
from tests.agent import scripted

_LOCK_QUOTE = 'inventory-db Lock wait timeout'


@pytest.fixture
def memory_session(field_ingest, tmp_path):
    """A shared result store and registered memory tool, as roles will use."""
    memory = store.MemoryStore(tmp_path / 'memory.sqlite3')
    memory.save(
        store.MemoryRecord(
            run_id='past',
            symptom_summary='lock wait timeout',
            diagnosed_root_cause='inventory-db',
            outcome='confirmed',
        )
    )
    session = scripted.make_session(
        field_ingest,
        fake.ScriptedClient([]),
        tmp_path,
        agent_config=config.AgentConfig(memory=True, verify=False),
    )
    session.executor = executor.ToolExecutor(
        tools.create_default_registry(field_ingest, memory_store=memory),
        session.executor.store,
        session.run_id,
    )
    return session, memory


@pytest.mark.parametrize('role', ['tester', 'skeptic', 'unknown'])
def test_other_roles_cannot_dispatch_memory(memory_session, role, monkeypatch):
    """A fabricated call cannot bypass the advertised role tools."""
    session, memory = memory_session

    def forbidden(*args, **kwargs):
        raise AssertionError('Unauthorized role reached memory')

    monkeypatch.setattr(memory, 'search', forbidden)
    client = fake.ScriptedClient(
        [
            scripted.call('search_past_incidents', query='lock'),
            scripted.submit(outcome='no_fault_found'),
        ]
    )
    session.client = client
    investigate.investigate(
        session,
        investigate.Brief(role=role),
        scripted.conversation(session),
        investigate.submit_tool(session),
    )

    assert 'search_past_incidents' not in {
        tool.name for tool in client.requests[0][0].tools
    }
    reply = next(
        message
        for message in client.requests[1][0].history
        if message.role == 'tool'
    )
    assert json.loads(str(reply.text))['error']['code'] == 'unknown_tool'
    assert len(list(session.executor.store)) == 0


@pytest.mark.parametrize(
    'role,node',
    [('agent', 'investigate'), ('lead', 'lead'), ('lead', 'correction')],
)
def test_lead_lookup_uses_shared_budget_and_role_tag(
    memory_session, role, node
):
    """Single-agent and lead passes use the same charged memory tool path."""
    session, _ = memory_session
    session.client = fake.ScriptedClient(
        [
            scripted.call('search_past_incidents', query='lock'),
            scripted.submit(outcome='no_fault_found'),
        ]
    )
    investigate.investigate(
        session,
        investigate.Brief(role=role, node=node),
        scripted.conversation(session),
        investigate.submit_tool(session),
    )
    event = next(
        event for event in session.record.events if event['type'] == 'tool_call'
    )

    assert event['role'] == role
    assert event['node'] == node
    assert event['tool'] == 'search_past_incidents'
    assert session.budget.tool_calls == 1


@pytest.mark.parametrize(
    'role,allowed', [('tester', False), ('skeptic', False), ('lead', True)]
)
def test_read_result_cannot_bypass_role_policy(memory_session, role, allowed):
    """Knowing a lead's call ID never exposes memory to another role."""
    session, _ = memory_session
    previous = session.executor.call('search_past_incidents', {'query': 'lock'})
    client = fake.ScriptedClient(
        [
            scripted.call('read_result', call_id=previous.call_id),
            scripted.submit(outcome='no_fault_found'),
        ]
    )
    session.client = client
    investigate.investigate(
        session,
        investigate.Brief(role=role),
        scripted.conversation(session),
        investigate.submit_tool(session),
    )
    reply = next(
        message
        for message in client.requests[1][0].history
        if message.role == 'tool'
    )
    result = json.loads(str(reply.text))

    assert result['ok'] is allowed
    if not allowed:
        assert result['error']['code'] == 'forbidden'
        assert 'inventory-db' not in str(reply.text)


def _seeded_store(tmp_path: Path, *, frozen: bool = False) -> store.MemoryStore:
    db = store.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(
        store.MemoryRecord(
            run_id='past-lock',
            symptom_summary='lock wait timeout on inventory-db',
            affected_components=('inventory-db',),
            diagnosed_root_cause='inventory-db',
            fault_type='lock_contention',
            key_evidence=('service: Lock wait timeout',),
            outcome='confirmed',
            what_fixed_it='secret-memory-fix: widen connection pool',
        )
    )
    return store.MemoryStore(db.path, frozen=frozen) if frozen else db


def _ledger_entries(result: graph.RunResult) -> dict[str, dict[str, object]]:
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


def test_multi_mode_lead_lookup_and_current_evidence_support(tmp_path):
    """Lead looks up memory, keeps origin='memory', and data supports."""
    memory = _seeded_store(tmp_path, frozen=True)
    client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_past_incidents', query='lock'),
                scripted.add_hypothesis(
                    'inventory-db',
                    origin='memory',
                    status='open',
                ),
                scripted.dispatch('H1'),
                scripted.submit('inventory-db', confidence='medium'),
            ],
            'tester:H1': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.verdict('H1', 'supported', _LOCK_QUOTE),
            ],
            'skeptic': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.challenge('H1', 'holds', _LOCK_QUOTE),
            ],
        }
    )
    settings = config.AgentConfig(mode='multi', memory=True, memory_frozen=True)

    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        settings,
        runs_dir=tmp_path / 'runs',
        run_id='run-multi-mem',
        memory_store=memory,
    )

    assert result.report.outcome == 'verified'
    assert result.report.ranks[0] == 'inventory-db'
    entries = _ledger_entries(result)
    assert entries['H1']['origin'] == 'memory'
    assert entries['H1']['status'] == 'supported'
    assert entries['H1']['tester_verdict'] == 'supported'

    tester_text = str(client.of('tester:H1')[0].history[0].text)
    assert 'secret-memory-fix' not in tester_text
    assert 'past-lock' not in tester_text

    skeptic_text = str(client.of('skeptic')[0].history[0].text)
    assert 'secret-memory-fix' not in skeptic_text
    assert 'search_past_incidents' not in skeptic_text

    memory_calls = [
        event
        for event in result.events
        if event['type'] == 'tool_call'
        and event['tool'] == 'search_past_incidents'
    ]
    assert len(memory_calls) == 1
    assert memory_calls[0]['role'] == 'lead'
    assert memory_calls[0]['node'] == 'lead'
    assert result.report.counters['tool_calls'] == 4
    assert len(memory.list_all()) == 1


def test_multi_mode_forged_memory_and_read_result_blocked(
    tmp_path, monkeypatch
):
    """Tester and skeptic forged search and read_result calls never leak."""
    memory = _seeded_store(tmp_path, frozen=True)
    real_search = memory.search
    search_roles: list[str] = []

    def tracked_search(*args, **kwargs):
        search_roles.append('called')
        return real_search(*args, **kwargs)

    monkeypatch.setattr(memory, 'search', tracked_search)
    client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_past_incidents', query='lock'),
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(
                    'inventory-db',
                    evidence=[
                        {
                            'call_id': 'call_0002',
                            'locator': 'service',
                            'quote': _LOCK_QUOTE,
                        }
                    ],
                    origin='memory',
                ),
                scripted.dispatch('H1'),
                scripted.submit('inventory-db'),
            ],
            'tester:H1': [
                scripted.call('search_past_incidents', query='lock'),
                scripted.call('read_result', call_id='call_0001'),
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.verdict('H1', 'supported', _LOCK_QUOTE),
            ],
            'skeptic': [
                scripted.call('search_past_incidents', query='lock'),
                scripted.call('read_result', call_id='call_0001'),
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.challenge('H1', 'holds', _LOCK_QUOTE),
            ],
        }
    )
    settings = config.AgentConfig(mode='multi', memory=True, memory_frozen=True)

    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        settings,
        runs_dir=tmp_path / 'runs',
        run_id='run-forged',
        memory_store=memory,
    )

    assert result.report.outcome == 'verified'
    assert len(search_roles) == 1

    for key in ('tester:H1', 'skeptic'):
        reqs = client.of(key)
        assert 'search_past_incidents' not in {t.name for t in reqs[0].tools}
        tool_msgs = [m for m in reqs[2].history if m.role == 'tool']
        assert json.loads(str(tool_msgs[0].text))['error']['code'] == (
            'unknown_tool'
        )
        forbidden = json.loads(str(tool_msgs[1].text))
        assert forbidden['error']['code'] == 'forbidden'
        assert 'secret-memory-fix' not in str(tool_msgs[1].text)


def test_multi_mode_memory_only_support_is_rejected(tmp_path):
    """Memory alone cannot support a hypothesis, verdict, or verification."""
    memory = _seeded_store(tmp_path, frozen=True)
    client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_past_incidents', query='lock'),
                scripted.add_hypothesis(
                    'inventory-db',
                    status='supported',
                    origin='memory',
                    evidence=[
                        {
                            'call_id': 'call_0001',
                            'locator': 'inventory-db',
                            'quote': 'lock wait timeout',
                        }
                    ],
                ),
                scripted.dispatch('H1'),
                scripted.submit('inventory-db'),
            ],
            'tester:H1': [
                scripted.call(
                    roles.VERDICT_NAME,
                    hypothesis_id='H1',
                    verdict='supported',
                    evidence=[
                        {
                            'call_id': 'call_0001',
                            'locator': 'inventory-db',
                            'quote': 'lock wait timeout',
                        }
                    ],
                ),
                scripted.verdict('H1', 'undecided', reason='only memory'),
            ],
        }
    )
    settings = config.AgentConfig(mode='multi', memory=True, memory_frozen=True)

    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        settings,
        runs_dir=tmp_path / 'runs',
        run_id='run-mem-only',
        memory_store=memory,
    )

    # Lead's status='supported' was refused and tester's memory citation failed.
    entries = _ledger_entries(result)
    assert entries['H1']['origin'] == 'memory'
    assert entries['H1']['status'] == 'open'
    assert entries['H1']['tester_verdict'] == 'undecided'
    assert not client.of('skeptic')
    assert result.report.outcome == 'inconclusive'

    rejected_verdicts = [
        e for e in result.events if e['type'] == 'submit_rejected'
    ]
    assert rejected_verdicts and rejected_verdicts[0]['role'] == 'tester'

    # Even direct verdict merging preserves origin and refuses memory support.
    direct_store = results.ResultStore()
    direct_store.add(
        results.StoredResult(
            call_id='call_0001',
            run_id='direct',
            tool='search_past_incidents',
            args={'query': 'lock'},
            ok=True,
            result={'leads': [{'diagnosed_root_cause': 'inventory-db'}]},
        )
    )
    book = ledger.HypothesisLedger('direct', direct_store)
    book.apply(
        {
            'operations': [
                {
                    'op': 'add',
                    'statement': 'from memory',
                    'component': 'inventory-db',
                    'origin': 'memory',
                }
            ]
        }
    )
    book.apply_verdict(
        'H1',
        'supported',
        confidence='high',
        evidence=[
            report.EvidenceRef(
                'call_0001', 'inventory-db', 'inventory-db', 'for'
            )
        ],
        reason=None,
    )
    assert book.entries['H1'].origin == 'memory'
    assert book.entries['H1'].status == 'open'
    assert not book.entries['H1'].evidence_for

    # Defense in depth: skeptic_opening never renders a memory-stored result.
    synthetic = ledger.Hypothesis(
        id='H9',
        statement='synthetic',
        component='inventory-db',
        origin='memory',
        evidence_for=[
            report.EvidenceRef(
                'call_0001', 'inventory-db', 'inventory-db', 'for'
            )
        ],
    )
    opening = roles.skeptic_opening(
        synthetic, direct_store, tool_calls=2, direction=''
    )
    assert 'search_past_incidents' not in opening
    assert 'call_0001' not in opening


def test_multi_mode_correction_pass_offers_memory_to_lead(tmp_path):
    """After skeptic disproves H1, lead_report still offers memory to lead."""
    memory = _seeded_store(tmp_path, frozen=True)
    client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(
                    'inventory-db', evidence=[scripted.LOCK_EVIDENCE]
                ),
                scripted.dispatch('H1'),
                scripted.call('search_past_incidents', query='lock'),
                scripted.submit('inventory-db'),
            ],
            'tester:H1': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.verdict('H1', 'supported', _LOCK_QUOTE),
            ],
            'skeptic': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.challenge('H1', 'disproved', _LOCK_QUOTE),
            ],
        }
    )
    settings = config.AgentConfig(mode='multi', memory=True, memory_frozen=True)

    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        settings,
        runs_dir=tmp_path / 'runs',
        run_id='run-correct-mem',
        memory_store=memory,
    )

    correction_calls = [
        event
        for event in result.events
        if event['type'] == 'tool_call'
        and event['role'] == 'lead'
        and event['node'] == 'lead_report'
        and event['tool'] == 'search_past_incidents'
    ]
    assert len(correction_calls) == 1
    assert 'search_past_incidents' in {
        tool.name for tool in client.of('lead')[-1].tools
    }
    assert (
        int(str(result.report.counters['tool_calls']))
        <= settings.limits.tool_calls
    )


def test_multi_mode_persistence_and_continuation_retain_restrictions(tmp_path):
    """Multi mode writes one record/run, keeps feedback, obeys frozen."""
    writable = store.MemoryStore(tmp_path / 'writable.sqlite3')
    client = scripted.RoutedClient(
        {
            'lead': [
                # Pass 0
                scripted.call('search_past_incidents', query='lock'),
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(
                    'inventory-db',
                    evidence=[
                        {
                            'call_id': 'call_0002',
                            'locator': 'service',
                            'quote': _LOCK_QUOTE,
                        }
                    ],
                ),
                scripted.dispatch('H1'),
                scripted.submit('inventory-db'),
                # Pass 1 (continuation)
                scripted.call('search_past_incidents', query='lock'),
                scripted.dispatch('H1'),
                scripted.submit('inventory-db'),
            ],
            'tester:H1': [
                # Pass 0
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.verdict('H1', 'supported', _LOCK_QUOTE),
                # Pass 1 (continuation): tries reading pass-0 memory call
                scripted.call('search_past_incidents', query='lock'),
                scripted.call('read_result', call_id='call_0001'),
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.verdict('H1', 'supported', _LOCK_QUOTE),
            ],
            'skeptic': [
                # Pass 0
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.challenge('H1', 'holds', _LOCK_QUOTE),
                # Pass 1 (continuation): tries reading pass-0 memory call
                scripted.call('read_result', call_id='call_0001'),
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.challenge('H1', 'holds', _LOCK_QUOTE),
            ],
        }
    )
    settings = config.AgentConfig(
        mode='multi', memory=True, memory_frozen=False
    )

    first = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        settings,
        runs_dir=tmp_path / 'runs',
        run_id='run-cont-multi',
        memory_store=writable,
    )
    assert first.run_dir is not None
    assert len(writable.list_all()) == 1
    saved = writable.get('run-cont-multi')
    assert saved is not None and saved.outcome == 'unverified'

    writable.update_outcome(
        'run-cont-multi', 'confirmed', what_fixed_it='Restart inventory-db'
    )
    extra = tmp_path / 'notes.md'
    extra.write_text('# Notes\n\nStill seeing lock waits.\n', encoding='utf-8')

    continued = continuation.continue_run(
        first.run_dir, [extra], client, memory_store=writable
    )

    assert len(writable.list_all()) == 1
    after = writable.get('run-cont-multi')
    assert after is not None
    assert after.outcome == 'confirmed'
    assert after.what_fixed_it == 'Restart inventory-db'

    pass1_tester = client.of('tester:H1')[2:]
    assert (
        json.loads(
            str(
                [m for m in pass1_tester[1].history if m.role == 'tool'][0].text
            )
        )['error']['code']
        == 'unknown_tool'
    )
    assert (
        json.loads(
            str(
                [m for m in pass1_tester[2].history if m.role == 'tool'][1].text
            )
        )['error']['code']
        == 'forbidden'
    )

    pass1_skeptic = client.of('skeptic')[2:]
    assert (
        json.loads(
            str(
                [m for m in pass1_skeptic[1].history if m.role == 'tool'][
                    0
                ].text
            )
        )['error']['code']
        == 'forbidden'
    )
    assert continued.report.outcome == 'verified'

    # Frozen continuation writes nothing even when memory_frozen=True.
    frozen = store.MemoryStore(writable.path, frozen=True)
    frozen_settings = config.AgentConfig(
        mode='multi', memory=True, memory_frozen=True
    )
    frozen_client = scripted.RoutedClient(
        {
            'lead': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(
                    'inventory-db', evidence=[scripted.LOCK_EVIDENCE]
                ),
                scripted.dispatch('H1'),
                scripted.submit('inventory-db'),
            ],
            'tester:H1': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.verdict('H1', 'supported', _LOCK_QUOTE),
            ],
            'skeptic': [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.challenge('H1', 'holds', _LOCK_QUOTE),
            ],
        }
    )
    frozen_run = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        frozen_client,
        frozen_settings,
        runs_dir=tmp_path / 'runs',
        run_id='run-frozen-multi',
        memory_store=frozen,
    )
    assert frozen_run.report.outcome == 'verified'
    assert writable.get('run-frozen-multi') is None
