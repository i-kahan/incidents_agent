"""Unit and integration tests for incident memory."""

import json
import pathlib
import shutil

import pytest

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.agent import config
from incidents_agent.agent import continuation
from incidents_agent.agent import graph
from incidents_agent.agent import ledger
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import state
from incidents_agent.llm import config as llm_config
from incidents_agent.llm import fake
from incidents_agent.memory import store as incident_memory
from tests.agent import scripted

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _record(
    run_id: str,
    symptom_summary: str,
    *,
    affected_components: tuple[str, ...] = ('inventory-db',),
    diagnosed_root_cause: str | None = 'inventory-db',
    fault_type: str | None = 'lock_contention',
    key_evidence: tuple[str, ...] = ('service: Lock wait timeout',),
    recommended_actions: tuple[str, ...] = ('Inspect DB locks',),
    outcome: incident_memory.MemoryOutcome = 'unverified',
    what_fixed_it: str | None = None,
    created_at: str = '2026-11-01T10:00:00+00:00',
    updated_at: str = '2026-11-01T10:00:00+00:00',
) -> incident_memory.MemoryRecord:
    return incident_memory.MemoryRecord(
        run_id=run_id,
        symptom_summary=symptom_summary,
        affected_components=affected_components,
        diagnosed_root_cause=diagnosed_root_cause,
        fault_type=fault_type,
        key_evidence=key_evidence,
        recommended_actions=recommended_actions,
        outcome=outcome,
        what_fixed_it=what_fixed_it,
        created_at=created_at,
        updated_at=updated_at,
    )


def test_memory_record_round_trips_through_store(
    tmp_path: pathlib.Path,
) -> None:
    """Saving and reading back a record preserves every field."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    record = _record(
        'run-001',
        'High lock wait timeout on checkout requests',
        affected_components=('inventory-db', 'order-service'),
        diagnosed_root_cause='inventory-db',
        fault_type='db_lock',
        key_evidence=('service: Lock wait timeout exceeded 500ms',),
        recommended_actions=('Kill blocking query', 'Add index'),
        outcome='confirmed',
        what_fixed_it='Added missing index on stock_items',
    )

    db.save(record)
    loaded = db.get('run-001')
    listed = db.list_all()
    dict_rebuilt = incident_memory.MemoryRecord.from_dict(
        json.loads(json.dumps(record.to_dict()))
    )

    assert loaded == record
    assert listed == [record]
    assert dict_rebuilt == record


@pytest.mark.parametrize(
    ('kwargs', 'message'),
    [
        pytest.param({'run_id': '  '}, 'run_id', id='empty_run_id'),
        pytest.param(
            {'symptom_summary': ''}, 'symptom_summary', id='empty_summary'
        ),
        pytest.param({'outcome': 'invalid'}, 'outcome', id='bad_outcome'),
    ],
)
def test_memory_record_rejects_invalid_fields(
    kwargs: dict[str, object], message: str
) -> None:
    """Invalid required fields or outcomes raise ValueError."""
    base: dict[str, object] = {
        'run_id': 'run-1',
        'symptom_summary': 'cpu spike on checkout',
        'outcome': 'unverified',
    }
    base.update(kwargs)

    with pytest.raises(ValueError, match=message):
        incident_memory.MemoryRecord.from_dict(base)


def test_memory_store_get_missing_returns_none(
    tmp_path: pathlib.Path,
) -> None:
    """Looking up an absent run id returns None."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')

    assert db.get('missing-run') is None


def test_memory_store_save_replaces_existing_record(
    tmp_path: pathlib.Path,
) -> None:
    """Saving with an existing run_id replaces the stored record."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(_record('run-1', 'initial symptom', outcome='unverified'))
    updated = _record('run-1', 'refined symptom', outcome='confirmed')

    db.save(updated)

    assert db.list_all() == [updated]


def test_search_bm25_ranks_matching_symptom_summary_first(
    tmp_path: pathlib.Path,
) -> None:
    """BM25 ranks the record sharing the most specific symptom terms first."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(
        _record(
            'r-low',
            'Minor latency increase during batch job',
            affected_components=('worker',),
            diagnosed_root_cause='worker',
            outcome='confirmed',
        )
    )
    db.save(
        _record(
            'r-best',
            'Database lock wait timeout exhausted connection pool',
            affected_components=('postgres',),
            diagnosed_root_cause='postgres',
            outcome='confirmed',
        )
    )
    db.save(
        _record(
            'r-partial',
            'Connection pool reset after deploy',
            affected_components=('gateway',),
            diagnosed_root_cause='gateway',
            outcome='confirmed',
        )
    )

    result = db.search('lock wait timeout connection pool')

    assert [m.record.run_id for m in result.leads] == ['r-best', 'r-partial']
    assert result.leads[0].bm25_score > result.leads[1].bm25_score


def test_search_component_boost_elevates_component_match(
    tmp_path: pathlib.Path,
) -> None:
    """A matching component name boosts the record above keyword-only hits."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(
        _record(
            'r-keyword-only',
            'Request latency spike error spike timeout spike',
            affected_components=('payment-service',),
            diagnosed_root_cause='payment-service',
            outcome='confirmed',
        )
    )
    db.save(
        _record(
            'r-component',
            'Latency spike observed',
            affected_components=('inventory-db',),
            diagnosed_root_cause='inventory-db',
            outcome='confirmed',
        )
    )

    result = db.search('latency spike timeout', components=['inventory-db'])

    assert [m.record.run_id for m in result.leads] == [
        'r-component',
        'r-keyword-only',
    ]
    assert result.leads[0].matched_components == ('inventory-db',)
    assert result.leads[0].score >= (
        result.leads[0].bm25_score + incident_memory.COMPONENT_BOOST
    )


def test_search_excludes_current_run_id(tmp_path: pathlib.Path) -> None:
    """The current run is excluded from search matches."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(_record('current-run', 'lock wait timeout', outcome='confirmed'))
    db.save(_record('past-run', 'lock wait timeout', outcome='confirmed'))

    result = db.search('lock wait timeout', exclude_run_id='current-run')

    assert result.total_records == 1
    assert [m.record.run_id for m in result.leads] == ['past-run']


def test_search_tool_separates_leads_and_counterevidence_and_hides_unchecked(
    tmp_path: pathlib.Path,
) -> None:
    """Confirmed leads hide unchecked records; rejected ones warn separately."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(
        _record(
            'r-confirmed',
            'Lock wait timeout on inventory queries',
            affected_components=('inventory-db',),
            diagnosed_root_cause='inventory-db',
            outcome='confirmed',
            what_fixed_it='Terminated stale migration transaction',
        )
    )
    db.save(
        _record(
            'r-rejected',
            'Lock wait timeout blamed on gateway saturation',
            affected_components=('api-gateway',),
            diagnosed_root_cause='api-gateway',
            outcome='rejected',
            what_fixed_it='Gateway restart did not help; DB lock was the cause',
        )
    )
    db.save(
        _record(
            'r-unverified',
            'Lock wait timeout saw cache miss',
            affected_components=('redis',),
            diagnosed_root_cause='redis',
            outcome='unverified',
        )
    )

    reply = incident_memory.search_past_incidents(
        db, {'query': 'lock wait timeout'}
    )

    assert reply['ok'] is True
    envelope = reply['untrusted_data']
    assert isinstance(envelope, dict)
    assert envelope['_untrusted_source'] is True
    content = envelope['content']
    assert isinstance(content, dict)
    leads = content['leads']
    counter = content['counterevidence']
    unchecked = content['unchecked']
    assert isinstance(leads, list) and len(leads) == 1
    assert leads[0]['incident_id'] == 'r-confirmed'
    assert leads[0]['what_fixed_it'] == (
        'Terminated stale migration transaction'
    )
    assert isinstance(counter, list) and len(counter) == 1
    assert counter[0]['incident_id'] == 'r-rejected'
    assert 'This was tried and was wrong' in str(counter[0]['warning'])
    assert counter[0]['rejected_root_cause'] == 'api-gateway'
    assert unchecked == []
    assert reply['locator'] == {
        'kind': 'incident_memory',
        'expression': 'lock wait timeout',
    }
    assert reply['target_components'] == []


def test_search_tool_returns_unchecked_only_when_no_confirmed_match(
    tmp_path: pathlib.Path,
) -> None:
    """Unverified records appear as unchecked when no confirmed lead hits."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(
        _record(
            'r-confirmed-other',
            'Disk full on analytics worker',
            affected_components=('batch-worker',),
            diagnosed_root_cause='batch-worker',
            outcome='confirmed',
        )
    )
    db.save(
        _record(
            'r-unverified',
            'Lock wait timeout on inventory queries',
            outcome='unverified',
        )
    )

    reply = incident_memory.search_past_incidents(
        db, {'query': 'lock wait timeout'}
    )

    envelope = reply['untrusted_data']
    assert isinstance(envelope, dict)
    content = envelope['content']
    assert isinstance(content, dict)
    unchecked = content['unchecked']
    assert content['leads'] == []
    assert isinstance(unchecked, list) and len(unchecked) == 1
    assert unchecked[0]['incident_id'] == 'r-unverified'
    assert unchecked[0]['status'] == 'unchecked'


@pytest.mark.parametrize(
    'args',
    [
        pytest.param({'query': ''}, id='empty_query'),
        pytest.param({'query': '---'}, id='no_tokens'),
        pytest.param(
            {'query': 'lock', 'components': 'db'}, id='bad_components'
        ),
        pytest.param({'query': 'lock', 'top_k': True}, id='bool_top_k'),
    ],
)
def test_search_tool_rejects_invalid_args(
    tmp_path: pathlib.Path, args: dict[str, object]
) -> None:
    """Invalid arguments return a structured tool error."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')

    reply = incident_memory.search_past_incidents(db, args)

    error = reply['error']
    assert reply['ok'] is False
    assert isinstance(error, dict)
    assert error['code'] == 'invalid_args'


def test_update_outcome_updates_record_and_preserves_created_at(
    tmp_path: pathlib.Path,
) -> None:
    """Feedback updates outcome and fix text while keeping creation time."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(
        _record(
            'run-1',
            'High CPU on checkout',
            outcome='unverified',
            created_at='2026-11-01T10:00:00+00:00',
            updated_at='2026-11-01T10:00:00+00:00',
        )
    )

    updated = db.update_outcome(
        'run-1',
        'confirmed',
        what_fixed_it='Rolled back v2.14 deploy',
        updated_at='2026-11-02T12:00:00+00:00',
    )

    assert updated.outcome == 'confirmed'
    assert updated.what_fixed_it == 'Rolled back v2.14 deploy'
    assert updated.created_at == '2026-11-01T10:00:00+00:00'
    assert updated.updated_at == '2026-11-02T12:00:00+00:00'
    assert db.get('run-1') == updated


def test_update_outcome_unknown_run_raises_key_error(
    tmp_path: pathlib.Path,
) -> None:
    """Updating an unknown run id raises KeyError."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')

    with pytest.raises(KeyError, match='missing'):
        db.update_outcome('missing', 'confirmed')


def test_delete_removes_record_and_returns_boolean(
    tmp_path: pathlib.Path,
) -> None:
    """Deleting a record removes it; deleting a missing id returns False."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(_record('run-1', 'High CPU'))

    removed = db.delete('run-1')
    absent = db.delete('run-1')

    assert (removed, absent, db.get('run-1')) == (True, False, None)


def test_snapshot_hash_is_deterministic_and_tracks_changes(
    tmp_path: pathlib.Path,
) -> None:
    """Snapshot hash is insertion-order independent and changes on mutation."""
    db_a = incident_memory.MemoryStore(tmp_path / 'a.sqlite3')
    db_b = incident_memory.MemoryStore(tmp_path / 'b.sqlite3')
    r1 = _record('run-1', 'symptom one', outcome='confirmed')
    r2 = _record('run-2', 'symptom two', outcome='rejected')
    db_a.save(r1)
    db_a.save(r2)
    db_b.save(r2)
    db_b.save(r1)

    hash_before = db_a.snapshot_hash()
    assert hash_before == db_b.snapshot_hash()

    db_a.update_outcome('run-2', 'confirmed', what_fixed_it='Restarted worker')
    hash_after_update = db_a.snapshot_hash()
    db_a.delete('run-1')
    hash_after_delete = db_a.snapshot_hash()

    assert hash_after_update != hash_before
    assert hash_after_delete != hash_after_update


def test_frozen_store_allows_lookups_on_missing_and_existing_db(
    tmp_path: pathlib.Path,
) -> None:
    """Frozen mode reads existing data and never creates a missing database."""
    missing_path = tmp_path / 'absent' / 'memory.sqlite3'
    missing_frozen = incident_memory.MemoryStore(missing_path, frozen=True)
    assert missing_frozen.list_all() == []
    assert missing_frozen.get('run-1') is None
    assert not missing_path.exists()

    db_path = tmp_path / 'seeded.sqlite3'
    writable = incident_memory.MemoryStore(db_path)
    writable.save(_record('run-1', 'lock wait timeout', outcome='confirmed'))

    frozen = incident_memory.MemoryStore(db_path, frozen=True)
    found = frozen.search('lock wait timeout')

    assert frozen.get('run-1') is not None
    assert len(found.leads) == 1
    assert frozen.snapshot_hash() == writable.snapshot_hash()


@pytest.mark.parametrize(
    'operation',
    [
        pytest.param('save', id='save'),
        pytest.param('update_outcome', id='update_outcome'),
        pytest.param('delete', id='delete'),
    ],
)
def test_frozen_store_refuses_writes(
    tmp_path: pathlib.Path, operation: str
) -> None:
    """Every write method raises FrozenMemoryError when frozen is True."""
    db_path = tmp_path / 'memory.sqlite3'
    writable = incident_memory.MemoryStore(db_path)
    initial = _record('run-1', 'lock wait timeout', outcome='unverified')
    writable.save(initial)
    frozen = incident_memory.MemoryStore(db_path, frozen=True)

    with pytest.raises(incident_memory.FrozenMemoryError):
        if operation == 'save':
            frozen.save(_record('run-2', 'new record'))
        elif operation == 'update_outcome':
            frozen.update_outcome('run-1', 'confirmed', what_fixed_it='fix')
        else:
            frozen.delete('run-1')

    assert writable.list_all() == [initial]


def test_agent_config_memory_defaults_off_and_round_trips(
    tmp_path: pathlib.Path,
) -> None:
    """Memory flags default to False, validate booleans, and round-trip."""
    repo_cfg = config.load_agent_config(_REPO / 'config' / 'agent.yaml')
    assert (repo_cfg.memory, repo_cfg.memory_frozen) == (False, False)

    custom = config.AgentConfig(memory=True, memory_frozen=True)
    rebuilt = config.AgentConfig.from_dict(
        json.loads(json.dumps(custom.to_dict()))
    )
    assert rebuilt == custom

    bad_path = tmp_path / 'bad.yaml'
    bad_path.write_text('memory: yes_please\n', encoding='utf-8')
    with pytest.raises(llm_config.ConfigError, match='memory'):
        config.load_agent_config(bad_path)


@pytest.mark.integration
def test_registry_omits_memory_tool_when_store_is_none(
    tmp_path: pathlib.Path,
) -> None:
    """When memory_store is None, search_past_incidents is not registered."""
    ingested = ingest.ingest_folder(
        scripted.FIELD_CASE, workdir=tmp_path / 'db'
    )
    try:
        reg = tools.create_default_registry(ingested)

        assert incident_memory.SEARCH_TOOL_NAME not in reg.names()
    finally:
        ingested.close()


@pytest.mark.integration
def test_registry_registers_memory_tool_when_store_provided(
    tmp_path: pathlib.Path,
) -> None:
    """When memory_store is given, search_past_incidents runs via registry."""
    db = incident_memory.MemoryStore(tmp_path / 'memory.sqlite3')
    db.save(_record('past-1', 'lock wait timeout', outcome='confirmed'))
    ingested = ingest.ingest_folder(
        scripted.FIELD_CASE, workdir=tmp_path / 'db'
    )
    try:
        reg = tools.create_default_registry(
            ingested, memory_store=db, run_id='current'
        )
        res = reg.call(
            incident_memory.SEARCH_TOOL_NAME,
            {'query': 'lock wait timeout'},
        )

        summary = res['summary']
        assert incident_memory.SEARCH_TOOL_NAME in reg.names()
        assert res['ok'] is True
        assert isinstance(summary, dict)
        assert summary['leads_count'] == 1
    finally:
        ingested.close()


@pytest.mark.integration
def test_run_investigation_memory_off_omits_tool_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """With memory off, the tool is not offered and no store file is written."""
    client = fake.ScriptedClient([scripted.submit(outcome='no_fault_found')])
    runs_dir = tmp_path / 'runs'

    graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        config.AgentConfig(memory=False),
        runs_dir=runs_dir,
        run_id='run-off',
    )

    tool_names = {tool.name for tool in client.requests[0][0].tools}
    manifest = json.loads((runs_dir / 'run-off' / 'manifest.json').read_text())
    assert incident_memory.SEARCH_TOOL_NAME not in tool_names
    assert 'memory_snapshot_hash' not in manifest
    assert not (runs_dir / 'memory.sqlite3').exists()


@pytest.mark.integration
def test_run_investigation_memory_on_writes_unverified_record(
    tmp_path: pathlib.Path,
) -> None:
    """With memory on, a run saves an unverified record at completion."""
    client = fake.ScriptedClient(
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(
                evidence=[scripted.LOCK_EVIDENCE], fault_type='db_lock'
            ),
            scripted.submit('inventory-db', fault_type='db_lock'),
            scripted.call('dependency_graph'),
            scripted.call(
                graph.VERIFY_NAME,
                hypothesis_id='H1',
                check='check upstream',
                outcome='supported',
                evidence=[
                    {
                        'call_id': 'call_0002',
                        'locator': 'inventory-db',
                        'quote': 'order-service',
                    }
                ],
            ),
        ]
    )
    runs_dir = tmp_path / 'runs'
    mem_path = tmp_path / 'memory.sqlite3'

    result = graph.run_investigation(
        state.Task(
            scripted.FIELD_CASE,
            question='Checkout stalls with database lock waits',
        ),
        client,
        config.AgentConfig(memory=True),
        runs_dir=runs_dir,
        run_id='run-mem-on',
        memory_path=mem_path,
    )

    tool_names = {tool.name for tool in client.requests[0][0].tools}
    db = incident_memory.MemoryStore(mem_path, frozen=True)
    saved = db.get('run-mem-on')
    manifest = json.loads(
        (runs_dir / 'run-mem-on' / 'manifest.json').read_text()
    )
    assert incident_memory.SEARCH_TOOL_NAME in tool_names
    assert result.report.outcome == 'verified'
    assert 'memory_snapshot_hash' in manifest
    assert saved is not None
    assert saved.outcome == 'unverified'
    assert saved.diagnosed_root_cause == 'inventory-db'
    assert 'inventory-db' in saved.affected_components
    assert 'Checkout stalls' in saved.symptom_summary


@pytest.mark.integration
def test_run_investigation_frozen_memory_looks_up_without_writing(
    tmp_path: pathlib.Path,
) -> None:
    """Frozen mode allows lookup while refusing end-of-run writes."""
    mem_path = tmp_path / 'memory.sqlite3'
    seed = incident_memory.MemoryStore(mem_path)
    seed.save(
        _record(
            'seed-1',
            'Lock wait timeout caused by inventory-db contention',
            outcome='confirmed',
            what_fixed_it='Killed idle transaction',
        )
    )
    before_hash = seed.snapshot_hash()

    client = fake.ScriptedClient(
        [
            scripted.call(
                incident_memory.SEARCH_TOOL_NAME,
                query='lock wait timeout',
                components=['inventory-db'],
            ),
            scripted.submit(outcome='no_fault_found'),
        ]
    )

    graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        config.AgentConfig(memory=True, memory_frozen=True),
        runs_dir=tmp_path / 'runs',
        run_id='run-frozen',
        memory_path=mem_path,
    )

    assert seed.snapshot_hash() == before_hash
    assert seed.get('run-frozen') is None


@pytest.mark.integration
def test_continue_run_with_memory_updates_unverified_record(
    tmp_path: pathlib.Path,
) -> None:
    """Continuing a run with memory on updates the run's unverified record."""
    mem_path = tmp_path / 'memory.sqlite3'
    runs_dir = tmp_path / 'runs'
    folder = tmp_path / 'case'
    shutil.copytree(scripted.FIELD_CASE, folder)
    extra = tmp_path / 'extra.log'
    extra.write_text('1700001260 ERROR inventory-db deadlock\n', 'utf-8')

    first_client = fake.ScriptedClient(
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.submit('inventory-db'),
            scripted.call(
                graph.VERIFY_NAME,
                hypothesis_id='H1',
                check='no trace yet',
                outcome='inconclusive',
                evidence=[],
            ),
        ]
    )
    graph.run_investigation(
        state.Task(folder),
        first_client,
        config.AgentConfig(memory=True),
        runs_dir=runs_dir,
        run_id='run-cont',
        memory_path=mem_path,
    )
    db = incident_memory.MemoryStore(mem_path, frozen=True)
    first_record = db.get('run-cont')
    assert first_record is not None

    cont_client = fake.ScriptedClient(
        [
            scripted.call('search_text', table='extra', pattern='deadlock'),
            scripted.submit('inventory-db', fault_type='deadlock'),
            scripted.call('dependency_graph'),
            scripted.call(
                graph.VERIFY_NAME,
                hypothesis_id='H1',
                check='confirmed upstream after continuation',
                outcome='supported',
                evidence=[
                    {
                        'call_id': 'call_0003',
                        'locator': 'inventory-db',
                        'quote': 'order-service',
                    }
                ],
            ),
        ]
    )
    continuation.continue_run(
        runs_dir / 'run-cont',
        [extra],
        cont_client,
        memory_path=mem_path,
    )

    updated_record = db.get('run-cont')
    assert updated_record is not None
    assert updated_record.created_at == first_record.created_at
    assert updated_record.fault_type == 'deadlock'
    assert updated_record.outcome == 'unverified'


@pytest.mark.integration
def test_memory_origin_hypothesis_needs_current_data_before_supported(
    tmp_path: pathlib.Path,
) -> None:
    """Memory lookup cannot support or verify without current data."""
    mem_path = tmp_path / 'memory.sqlite3'
    seed = incident_memory.MemoryStore(mem_path)
    seed.save(
        _record(
            'seed-1',
            'Lock wait timeout on inventory-db queries',
            affected_components=('inventory-db',),
            diagnosed_root_cause='inventory-db',
            outcome='confirmed',
            what_fixed_it='Killed blocking transaction',
        )
    )
    memory_citation: dict[str, object] = {
        'call_id': 'call_0001',
        'locator': 'inventory-db',
        'quote': 'Lock wait timeout on inventory-db queries',
    }
    data_citation = {
        'call_id': 'call_0002',
        'locator': 'service',
        'quote': 'inventory-db Lock wait timeout',
    }
    verify_citation = {
        'call_id': 'call_0003',
        'locator': 'inventory-db',
        'quote': 'order-service',
    }

    client = fake.ScriptedClient(
        [
            scripted.call(
                incident_memory.SEARCH_TOOL_NAME,
                query='lock wait timeout',
                components=['inventory-db'],
            ),
            scripted.add_hypothesis(
                component='inventory-db',
                origin='memory',
                status='supported',
                evidence=[memory_citation],
            ),
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.call(
                ledger.UPDATE_TOOL_NAME,
                operations=[
                    {
                        'op': 'revise',
                        'id': 'H1',
                        'status': 'supported',
                        'evidence': [data_citation],
                    }
                ],
            ),
            scripted.submit('inventory-db'),
            scripted.call(
                graph.VERIFY_NAME,
                hypothesis_id='H1',
                check='memory-only check should fail',
                outcome='supported',
                evidence=[memory_citation],
            ),
            scripted.call('dependency_graph'),
            scripted.call(
                graph.VERIFY_NAME,
                hypothesis_id='H1',
                check='upstream dependency check',
                outcome='supported',
                evidence=[verify_citation],
            ),
        ]
    )

    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        client,
        config.AgentConfig(memory=True, memory_frozen=True),
        runs_dir=tmp_path / 'runs',
        run_id='run-graph-mem',
        memory_path=mem_path,
    )

    ledger_events = [
        event for event in result.events if event['type'] == 'ledger'
    ]
    first_ledger = ledger_events[0]['ledger']
    first_result = ledger_events[0]['result']
    assert isinstance(first_ledger, dict)
    assert isinstance(first_result, dict)
    entries = first_ledger['entries']
    assert isinstance(entries, list)
    first_h1 = entries[0]
    rejected_verify = [
        event
        for event in result.events
        if event['type'] == 'submit_rejected' and event['node'] == 'verify'
    ]
    assert first_h1['status'] == 'open'
    assert first_h1['evidence_for'] == []
    assert 'current data' in str(first_result['errors'])
    assert len(rejected_verify) == 1
    assert result.report.outcome == 'verified'
    assert result.report.ranks == ('inventory-db',)
    assert {ref.call_id for ref in result.report.causes[0].evidence} == {
        'call_0002',
        'call_0003',
    }


def test_memory_counterevidence_can_attach_against_never_for() -> None:
    """Memory results can count against a hypothesis, never for one."""
    result_store = results.ResultStore()
    result_store.add(
        results.StoredResult(
            call_id='call_0001',
            run_id='run-x',
            tool='search_past_incidents',
            args={'query': 'lock wait'},
            ok=True,
            result={
                'ok': True,
                'untrusted_data': {
                    '_untrusted_source': True,
                    'notice': 'Untrusted.',
                    'content': {
                        'leads': [],
                        'counterevidence': [
                            {
                                'incident_id': 'past-9',
                                'rejected_root_cause': 'api-gateway',
                                'warning': 'This was tried and was wrong',
                            }
                        ],
                        'unchecked': [],
                    },
                },
                'locator': {
                    'kind': 'incident_memory',
                    'expression': 'lock wait',
                },
            },
            locator={'kind': 'incident_memory', 'expression': 'lock wait'},
        )
    )
    book = ledger.HypothesisLedger('run-x', result_store)

    book.apply(
        {
            'operations': [
                {
                    'op': 'add',
                    'statement': 'gateway caused it',
                    'component': 'api-gateway',
                    'origin': 'memory',
                    'evidence': [
                        {
                            'call_id': 'call_0001',
                            'locator': 'past-9',
                            'quote': 'This was tried and was wrong',
                            'stance': 'for',
                        },
                        {
                            'call_id': 'call_0001',
                            'locator': 'past-9',
                            'quote': 'This was tried and was wrong',
                            'stance': 'against',
                        },
                    ],
                }
            ]
        }
    )

    entry = book.entries['H1']
    assert entry.evidence_for == []
    assert entry.evidence_against == [
        report.EvidenceRef(
            'call_0001',
            'past-9',
            'This was tried and was wrong',
            'against',
        )
    ]
