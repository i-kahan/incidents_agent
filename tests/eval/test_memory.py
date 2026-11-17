"""Tuning-only seeding and immutable, content-keyed memory evaluation."""

import dataclasses
import json
import shutil
from concurrent import futures

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import graph
from incidents_agent.eval import agent_adapter
from incidents_agent.eval import answers
from incidents_agent.eval import memory_seed
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import split
from incidents_agent.llm import fake
from incidents_agent.memory import store
from scripts import seed_memory
from tests.agent import scripted


@pytest.fixture
def seed_inputs(evaluation_options, tmp_path):
    """Three tuning cases and separate tuning-only labels."""
    partition = split.from_json(runner.read_object(evaluation_options.split))
    tuning = tmp_path / 'tuning.answers.jsonl'
    answers.write_answers(
        answers.subset(
            answers.load_answers(evaluation_options.answers), partition.tuning
        ),
        tuning,
        evaluation_options.tasks_root,
    )
    return evaluation_options, tuning, partition


@pytest.fixture
def bundle(seed_inputs, tmp_path):
    """A complete, independently validated frozen bundle."""
    options, tuning, _ = seed_inputs
    output = tmp_path / 'seed'
    memory_seed.seed(options.split, options.tasks_root, tuning, output)
    return output


@pytest.fixture
def memory_options(seed_inputs, bundle):
    """Evaluate only the unseeded synthetic held-out case, with no payment."""
    options, _, _ = seed_inputs
    return dataclasses.replace(
        options,
        agent='single',
        cases='heldout',
        final=True,
        memory=True,
        memory_path=bundle,
    )


def test_seed_is_reproducible_and_tuning_only(seed_inputs, tmp_path):
    """Repeated builds use observed symptoms and deterministic timestamps."""
    options, tuning, partition = seed_inputs
    shutil.rmtree(options.tasks_root / 'cases/held')
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    manifest = memory_seed.seed(
        options.split, options.tasks_root, tuning, first
    )
    again = memory_seed.seed(options.split, options.tasks_root, tuning, second)
    memory = store.MemoryStore(first / 'memory.sqlite3', frozen=True)

    assert manifest == again
    assert manifest['case_ids'] == list(partition.tuning)
    assert {record.run_id for record in memory.list_all()} == {'a', 'b', 'c'}
    record = memory.get('a')
    assert record is not None
    assert record.outcome == 'confirmed'
    assert record.diagnosed_root_cause == 'svc-a'
    assert record.fault_type == 'cpu'
    assert record.what_fixed_it is None
    assert record.recommended_actions == ()
    assert record.created_at == '2023-11-14T22:18:20+00:00'
    assert record.updated_at == record.created_at
    assert 'svc-a_cpu' in record.symptom_summary
    assert record.key_evidence
    assert 'unavailable' in record.symptom_summary


def test_answer_changes_do_not_change_observed_symptoms(seed_inputs, tmp_path):
    """True labels fill diagnosis fields, never the searchable observations."""
    options, tuning, _ = seed_inputs
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    memory_seed.seed(options.split, options.tasks_root, tuning, first)
    tuning.write_text(tuning.read_text().replace('"cpu"', '"invented-fault"'))
    memory_seed.seed(options.split, options.tasks_root, tuning, second)
    before = store.MemoryStore(first / 'memory.sqlite3', frozen=True).get('a')
    after = store.MemoryStore(second / 'memory.sqlite3', frozen=True).get('a')

    assert before is not None and after is not None
    assert before.symptom_summary == after.symptom_summary
    assert before.key_evidence == after.key_evidence
    assert after.fault_type == 'invented-fault'
    assert 'invented-fault' not in after.symptom_summary


@pytest.mark.parametrize('bad', ['full', 'missing', 'duplicate', 'unknown'])
def test_bad_answers_never_publish(seed_inputs, tmp_path, bad):
    """Extra, missing and duplicated labels fail before any bundle is built."""
    options, tuning, _ = seed_inputs
    lines = tuning.read_text().splitlines()
    replacements = {
        'full': options.answers.read_text(),
        'missing': '\n'.join(lines[:2]),
        'duplicate': '\n'.join([*lines, lines[0]]),
        'unknown': '\n'.join(lines).replace('"a"', '"unknown"'),
    }
    tuning.write_text(replacements[bad])
    output = tmp_path / 'rejected'

    with pytest.raises(ValueError):
        memory_seed.seed(options.split, options.tasks_root, tuning, output)

    assert not output.exists()


@pytest.mark.parametrize('bad', ['mismatched_id', 'nan_time', 'huge_time'])
def test_invalid_task_leaves_no_partial_bundle(seed_inputs, tmp_path, bad):
    """A later invalid case removes the partially built staging database."""
    options, tuning, _ = seed_inputs
    task_path = options.tasks_root / 'cases/c/task.json'
    task = runner.read_object(task_path)
    changes: dict[str, dict[str, object]] = {
        'mismatched_id': {'case_id': 'held'},
        'nan_time': {'inject_time': float('nan')},
        'huge_time': {'inject_time': 1e100},
    }
    task.update(changes[bad])
    runner.atomic_json(task_path, task)

    with pytest.raises(ValueError):
        memory_seed.seed(
            options.split, options.tasks_root, tuning, tmp_path / 'bad'
        )

    assert not (tmp_path / 'bad').exists()
    assert list(tmp_path.glob('.memory-seed-*')) == []


def test_seed_refuses_existing_destination(seed_inputs, bundle):
    """Re-running seeding cannot overwrite a snapshot already in use."""
    options, tuning, _ = seed_inputs
    before = (bundle / 'memory.sqlite3').read_bytes()

    with pytest.raises(ValueError, match='already exists'):
        memory_seed.seed(options.split, options.tasks_root, tuning, bundle)

    assert (bundle / 'memory.sqlite3').read_bytes() == before


@pytest.mark.parametrize('inside', ['answers', 'output'])
def test_seed_artifacts_stay_outside_blind_view(seed_inputs, tmp_path, inside):
    """Neither labels nor labelled memory may live under the agent input."""
    options, tuning, _ = seed_inputs
    output = tmp_path / 'seed'
    if inside == 'answers':
        hidden = options.tasks_root / 'answers.jsonl'
        shutil.copyfile(tuning, hidden)
        tuning = hidden
    else:
        output = options.tasks_root / 'seed'

    with pytest.raises(ValueError, match='outside the blind task view'):
        memory_seed.seed(options.split, options.tasks_root, tuning, output)


def test_seed_cli_is_offline(seed_inputs, tmp_path, monkeypatch, capsys):
    """The CLI creates the complete bundle without invoking the agent."""
    options, tuning, _ = seed_inputs

    def forbidden(*args, **kwargs):
        raise AssertionError('Seeding invoked the agent')

    monkeypatch.setattr(graph, 'run_investigation', forbidden)
    assert (
        seed_memory.main(
            [
                '--split',
                str(options.split),
                '--tasks-root',
                str(options.tasks_root),
                '--answers',
                str(tuning),
                '--out',
                str(tmp_path / 'cli'),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result['records'] == 3
    assert len(result['memory_snapshot_hash']) == 64


def test_snapshot_is_isolated_from_source_edits(seed_inputs, bundle):
    """Workers read a private database, never a path another process updates."""
    _, _, partition = seed_inputs
    captured = memory_seed.capture(bundle, partition, ['held'])
    original = store.MemoryStore(bundle / 'memory.sqlite3')
    original.update_outcome('a', 'rejected')

    with futures.ThreadPoolExecutor(max_workers=3) as pool:
        hashes = list(
            pool.map(lambda _: captured.memory.snapshot_hash(), range(6))
        )

    assert hashes == [captured.digest] * 6
    assert captured.memory.path != original.path
    with pytest.raises(store.FrozenMemoryError):
        captured.memory.update_outcome('a', 'rejected')


@pytest.mark.parametrize(
    'bad', ['hash', 'split', 'ids', 'outcome', 'corrupt', 'missing']
)
def test_snapshot_rejects_invalid_bundle(seed_inputs, bundle, bad):
    """Invalid provenance and database contents fail before model creation."""
    _, _, partition = seed_inputs
    manifest_path = bundle / 'manifest.json'
    manifest = runner.read_object(manifest_path)
    if bad == 'outcome':
        store.MemoryStore(bundle / 'memory.sqlite3').update_outcome(
            'a', 'rejected'
        )
    elif bad == 'corrupt':
        (bundle / 'memory.sqlite3').write_bytes(b'not a database')
    elif bad == 'missing':
        (bundle / 'memory.sqlite3').unlink()
    else:
        changes: dict[str, dict[str, object]] = {
            'hash': {'memory_snapshot_hash': '0' * 64},
            'split': {'split_hash': '0' * 64},
            'ids': {'case_ids': ['held']},
        }
        manifest.update(changes[bad])
        runner.atomic_json(manifest_path, manifest)

    with pytest.raises(ValueError):
        memory_seed.capture(bundle, partition, ['held'])


def test_memory_eval_rejects_seeded_cases(memory_options):
    """A confirmed tuning answer cannot be retrieved while testing that case."""
    with pytest.raises(ValueError, match='overlap'):
        service.prepare(dataclasses.replace(memory_options, cases='tuning'))


@pytest.mark.parametrize(
    'change,match',
    [
        ({'memory_path': None}, 'requires --memory-path'),
        ({'memory': False}, 'requires memory'),
        ({'agent': 'single_call'}, 'does not support memory'),
    ],
)
def test_invalid_memory_options(memory_options, change, match):
    """Enablement, path and baseline selection must be consistent."""
    with pytest.raises(ValueError, match=match):
        service.prepare(dataclasses.replace(memory_options, **change))


def test_snapshot_identity_controls_cache(memory_options, bundle, tmp_path):
    """Paths do not affect cache identity; changed content and enablement do."""
    first = service.prepare(memory_options)
    moved = tmp_path / 'moved'
    shutil.copytree(bundle, moved)
    relocated = service.prepare(
        dataclasses.replace(memory_options, memory_path=moved)
    )
    disabled = service.prepare(
        dataclasses.replace(
            memory_options,
            memory=False,
            memory_path=None,
        )
    )
    writable = store.MemoryStore(moved / 'memory.sqlite3')
    record = writable.get('a')
    assert record is not None
    writable.save(
        dataclasses.replace(record, symptom_summary='Different symptoms')
    )
    manifest = runner.read_object(moved / 'manifest.json')
    manifest['memory_snapshot_hash'] = writable.snapshot_hash()
    runner.atomic_json(moved / 'manifest.json', manifest)
    changed = service.prepare(
        dataclasses.replace(memory_options, memory_path=moved)
    )

    assert first.manifest == relocated.manifest
    assert (
        first.manifest['configuration_fingerprint']
        != disabled.manifest['configuration_fingerprint']
    )
    assert (
        first.manifest['configuration_fingerprint']
        != changed.manifest['configuration_fingerprint']
    )


def test_seeded_lookup_reaches_agent_through_adapter(
    memory_options, bundle, tmp_path
):
    """The real graph can retrieve a frozen seed without changing its source."""
    prepared = service.prepare(memory_options)
    assert isinstance(prepared.executor, agent_adapter.Executor)
    client = fake.ScriptedClient(
        [
            scripted.call(
                'search_past_incidents', query='cpu', components=['svc-a']
            ),
            scripted.submit(outcome='no_fault_found'),
        ]
    )
    prepared.executor.client_factory = lambda _: client
    source = (bundle / 'memory.sqlite3').read_bytes()
    output = prepared.executor(
        prepared.cases[0], tmp_path / 'attempts', 'lookup'
    )
    events = (tmp_path / 'attempts/lookup/trajectory.jsonl').read_text()

    assert '"leads_count": 3' in events
    assert '"diagnosed_root_cause": "svc-a"' in events
    usage = output['usage']
    assert isinstance(usage, dict)
    assert usage['tool_calls'] >= 1
    assert (bundle / 'memory.sqlite3').read_bytes() == source
    assert prepared.snapshot is not None
    assert prepared.snapshot.memory.snapshot_hash() == prepared.snapshot.digest


def test_recovery_checks_memory_identity(memory_options, tmp_path):
    """A durable report cannot be recovered under another memory condition."""
    prepared = service.prepare(memory_options)
    executor = prepared.executor
    first = executor(prepared.cases[0], tmp_path / 'attempts', 'recover')
    recovered = executor(prepared.cases[0], tmp_path / 'attempts', 'recover')
    assert recovered['report'] == first['report']
    disabled = agent_adapter.Executor(
        lambda _: fake.ScriptedClient([]), config.AgentConfig()
    )

    with pytest.raises(ValueError, match='memory identity differs'):
        disabled(prepared.cases[0], tmp_path / 'attempts', 'recover')


def test_resume_rejects_different_snapshot(memory_options, bundle):
    """A frozen manifest prevents resuming under changed memory contents."""
    prepared = service.prepare(memory_options)
    completed = service.execute(prepared, compare_reference=False)
    memory = store.MemoryStore(bundle / 'memory.sqlite3')
    memory.update_outcome('a', 'confirmed', what_fixed_it='New confirmed fix')
    manifest = runner.read_object(bundle / 'manifest.json')
    manifest['memory_snapshot_hash'] = memory.snapshot_hash()
    runner.atomic_json(bundle / 'manifest.json', manifest)
    different = service.prepare(
        dataclasses.replace(memory_options, resume=completed.folder)
    )

    with pytest.raises(ValueError, match='Resume configuration'):
        service.execute(different, compare_reference=False)


@pytest.mark.parametrize(
    'inputs',
    [
        None,
        {},
        {'a': {}},
        {
            'a': {'task.json': 'broken'},
            'b': {'task.json': '0' * 64},
            'c': {'task.json': '0' * 64},
        },
    ],
)
def test_invalid_input_provenance(seed_inputs, bundle, inputs):
    """An untraceable seed is rejected even when its record hash matches."""
    _, _, partition = seed_inputs
    manifest = runner.read_object(bundle / 'manifest.json')
    manifest['inputs'] = inputs
    runner.atomic_json(bundle / 'manifest.json', manifest)

    with pytest.raises(ValueError, match='input'):
        memory_seed.capture(bundle, partition, ['held'])


def test_concurrent_seeders_do_not_overwrite(seed_inputs, tmp_path):
    """Only one publisher succeeds when two builders target the same bundle."""
    options, tuning, _ = seed_inputs
    output = tmp_path / 'contended'

    def publish() -> str:
        try:
            memory_seed.seed(options.split, options.tasks_root, tuning, output)
            return 'published'
        except ValueError as error:
            return str(error)

    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))

    assert sorted(results) == ['Output destination already exists', 'published']
    assert (
        len(
            store.MemoryStore(output / 'memory.sqlite3', frozen=True).list_all()
        )
        == 3
    )


def test_changed_inputs_never_publish(seed_inputs, tmp_path, monkeypatch):
    """Changing telemetry cannot publish mismatched provenance."""
    options, tuning, _ = seed_inputs
    real_hashes = runner.file_hashes
    observed = []

    def changing(folder):
        result = real_hashes(folder)
        observed.append(folder)
        if len(observed) == 2:
            result['task.json'] = '0' * 64
        return result

    monkeypatch.setattr(runner, 'file_hashes', changing)

    with pytest.raises(ValueError, match='changed while seeding'):
        memory_seed.seed(
            options.split, options.tasks_root, tuning, tmp_path / 'bad'
        )

    assert not (tmp_path / 'bad').exists()


def test_memory_eval_off_does_not_open_bundle(evaluation_options, monkeypatch):
    """Default evaluation has no path to memory or seed data."""

    def forbidden(*args, **kwargs):
        raise AssertionError('Memory was opened when disabled')

    monkeypatch.setattr(memory_seed, 'capture', forbidden)
    prepared = service.prepare(
        dataclasses.replace(evaluation_options, agent='single')
    )

    assert prepared.snapshot is None
    configuration = prepared.manifest['configuration']
    assert isinstance(configuration, dict)
    assert configuration['memory_snapshot_hash'] is None


def test_snapshot_change_before_recovery_is_refused(memory_options, tmp_path):
    """Even direct adapter callers cannot silently replace the private copy."""
    prepared = service.prepare(memory_options)
    assert prepared.snapshot is not None
    prepared.snapshot.memory.path.chmod(0o600)
    writable = store.MemoryStore(prepared.snapshot.memory.path)
    writable.update_outcome('a', 'rejected')

    with pytest.raises(ValueError, match='changed before attempt'):
        prepared.executor(prepared.cases[0], tmp_path / 'attempts', 'changed')
