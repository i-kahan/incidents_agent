"""Role access enforcement in the shared loop awaiting Phase 07's graph."""

import json

import pytest

from incidents_agent import tools
from incidents_agent.agent import config
from incidents_agent.agent import executor
from incidents_agent.agent import investigate
from incidents_agent.llm import fake
from incidents_agent.memory import store
from tests.agent import scripted


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
