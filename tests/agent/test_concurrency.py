"""Shared run objects used from several threads at once (Phase 07)."""

import threading
from collections.abc import Callable
from concurrent import futures
from pathlib import Path

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.agent import executor
from incidents_agent.agent import results
from incidents_agent.agent import trajectory
from tests.agent import scripted

_THREADS = 8
_PER_THREAD = 5


def _hammer(work: Callable[[int], object], count: int) -> None:
    """Run `work(index)` `count` times across threads, released together."""
    gate = threading.Barrier(_THREADS)

    def run(index: int) -> None:
        if index < _THREADS:
            gate.wait()
        work(index)

    with futures.ThreadPoolExecutor(_THREADS) as pool:
        list(pool.map(run, range(count)))


def test_executor_under_threads_gives_unique_ids_and_consistent_store(
    field_ingest: ingest.IngestResult,
) -> None:
    """Concurrent tool calls never share an id and are all stored."""
    store = results.ResultStore()
    tool_executor = executor.ToolExecutor(
        tools.create_default_registry(field_ingest), store, scripted.RUN_ID
    )
    made: list[str] = []
    lock = threading.Lock()

    def work(index: int) -> None:
        del index
        stored = tool_executor.call('search_text', scripted.LOCK_SEARCH)
        with lock:
            made.append(stored.call_id)

    _hammer(work, _THREADS * _PER_THREAD)

    assert len(set(made)) == _THREADS * _PER_THREAD
    assert sorted(made) == [item.call_id for item in store]
    assert all(item.ok for item in store)


def test_trajectory_under_threads_has_gap_free_sequence(
    tmp_path: Path,
) -> None:
    """Events recorded from threads get every seq once, in file order."""
    path = tmp_path / 'trajectory.jsonl'
    record = trajectory.Trajectory(path, scripted.RUN_ID)

    _hammer(
        lambda index: record.record('node', role='tester', index=index),
        _THREADS * _PER_THREAD,
    )

    written = trajectory.read_trajectory(path)
    expected = list(range(1, _THREADS * _PER_THREAD + 1))
    assert [event['seq'] for event in written] == expected
    assert [event['seq'] for event in record.events] == expected


def test_trajectory_writes_hypothesis_id_only_when_set(
    tmp_path: Path,
) -> None:
    """A tester event carries its hypothesis id; others carry none."""
    record = trajectory.Trajectory(None, scripted.RUN_ID)

    tester = record.record('node', role='tester', hypothesis_id='H2')
    lead = record.record('node', role='lead', hypothesis_id=None)

    assert (tester['role'], tester['hypothesis_id']) == ('tester', 'H2')
    assert 'hypothesis_id' not in lead


def test_result_store_iteration_is_a_snapshot() -> None:
    """Adding while iterating does not break the iteration."""
    store = results.ResultStore()
    store.add(_stored('call_0001'))
    walk = iter(store)

    store.add(_stored('call_0002'))

    assert [item.call_id for item in walk] == ['call_0001']
    assert len(store) == 2


def _stored(call_id: str) -> results.StoredResult:
    return results.StoredResult(
        call_id, scripted.RUN_ID, 'run_sql', {}, True, {'ok': True}
    )
