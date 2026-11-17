"""Unit and integration tests for `app.runtime`."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app import runtime
from app import view
from incidents_agent import ingest
from incidents_agent.agent import config as agent_config
from incidents_agent.agent import continuation
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.llm import fake
from tests.agent import scripted

FIELD_ROOT = Path(__file__).resolve().parents[2] / 'evalsets' / 'field'
SPLIT_PATH = Path(__file__).resolve().parents[2] / 'splits' / 're2-ob.json'


def _write_configs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    models = tmp_path / 'models.yaml'
    models.write_text(
        'models:\n'
        '  gpt-small:\n'
        '    model: openai/gpt-4o-mini\n'
        '  broken:\n'
        '    model: anthropic/<model-id>\n'
        'default: gpt-small\n',
        encoding='utf-8',
    )
    budget = tmp_path / 'budget.yaml'
    budget.write_text(
        'total_usd: 15.00\nsafety_margin_usd: 1.00\n', encoding='utf-8'
    )
    agent = tmp_path / 'agent.yaml'
    agent.write_text('model_alias: null\n', encoding='utf-8')
    env = tmp_path / '.env'
    env.write_text('OPENAI_API_KEY=sk-test-fake\n', encoding='utf-8')
    return models, budget, agent, env


def test_stage_uploads_and_cleanup_removes_session_folder(
    tmp_path: Path,
) -> None:
    """Uploaded bytes are staged per session and removed cleanly."""
    sess = runtime.create_session_dir(tmp_path)

    staged = runtime.stage_uploaded_files(
        sess, [('metrics.csv', b'time,cpu\n1700001000,10\n')]
    )

    assert (staged / 'metrics.csv').is_file()
    runtime.cleanup_session_dir(sess)
    assert not sess.exists()


def test_stage_uploaded_files_rejects_traversal_path(tmp_path: Path) -> None:
    """Path traversal filenames are refused during upload staging."""
    sess = runtime.create_session_dir(tmp_path)

    with pytest.raises(ValueError, match='Unsafe upload path'):
        runtime.stage_uploaded_files(sess, [('../escape.txt', b'bad')])


def test_list_and_resolve_cases_enforces_heldout_gate(
    tmp_path: Path,
) -> None:
    """Case discovery lists tuning/field cases and refuses held-out ids."""
    split_data = json.loads(SPLIT_PATH.read_text(encoding='utf-8'))
    heldout_id = str(split_data['heldout'][0])
    tuning_id = str(split_data['tuning'][0])
    tasks_root = tmp_path / 'tasks'
    (tasks_root / 'cases' / tuning_id).mkdir(parents=True)
    (tasks_root / 'cases' / heldout_id).mkdir(parents=True)

    available = runtime.list_available_cases(
        split_path=SPLIT_PATH,
        tasks_root=tasks_root,
        field_root=FIELD_ROOT,
    )

    assert tuning_id in available.benchmark_tuning
    assert heldout_id not in available.benchmark_tuning
    assert 'config_diff' in available.field_cases
    assert runtime.resolve_case_folder(
        tuning_id,
        suite='rcaeval',
        split_path=SPLIT_PATH,
        tasks_root=tasks_root,
        field_root=FIELD_ROOT,
    ).is_dir()
    with pytest.raises(ValueError, match='Held-out cases require --final'):
        runtime.resolve_case_folder(
            heldout_id,
            suite='rcaeval',
            split_path=SPLIT_PATH,
            tasks_root=tasks_root,
            field_root=FIELD_ROOT,
        )


def test_preview_ingest_applies_classification_and_time_overrides(
    tmp_path: Path,
) -> None:
    """Users can override file classification and fix missing time columns."""
    sess = runtime.create_session_dir(tmp_path)
    staged = runtime.stage_uploaded_files(
        sess,
        [
            ('ambiguous.txt', b'plain text line one\nplain text line two\n'),
            (
                'events.csv',
                b'raw_clock,val\n1700001000000,42\n1700001060000,99\n',
            ),
        ],
    )

    initial = runtime.preview_ingest(staged)
    assert initial.ready_for_time_analysis is False
    assert any(
        status.table_name == 'events' and status.needs_attention
        for status in initial.table_time_statuses
    )

    fixed = runtime.preview_ingest(
        staged,
        classification_overrides={'ambiguous.txt': ('log', 'none')},
        time_overrides={
            'ambiguous': runtime.TimeOverride(
                column=None, unit='unknown', confirmed=True
            ),
            'events': runtime.TimeOverride(
                column='raw_clock', unit='unix_ms', confirmed=True
            ),
        },
    )

    assert fixed.ready_for_time_analysis is True
    assert {tbl.name for tbl in fixed.catalog.tables} == {'ambiguous', 'events'}
    events_status = next(
        s for s in fixed.table_time_statuses if s.table_name == 'events'
    )
    assert events_status.time_usable is True
    assert events_status.confirmed is True
    assert events_status.min_time_s == pytest.approx(1700001000.0)


def test_inspect_runtime_status_filters_usable_models(
    tmp_path: Path,
) -> None:
    """Only models with keys and valid IDs appear in usable_aliases."""
    models, budget, agent, env = _write_configs(tmp_path)
    spend = tmp_path / 'spend.sqlite3'

    status = runtime.inspect_runtime_status(
        models_path=models,
        budget_path=budget,
        agent_path=agent,
        env_path=env,
        spend_path=spend,
    )

    assert status.usable_aliases == ('gpt-small',)
    assert status.default_alias == 'gpt-small'
    assert status.remaining_usd == Decimal('14.00')
    assert status.halted is False


@pytest.mark.integration
@pytest.mark.parametrize(
    'case_name',
    [
        pytest.param('bad_deploy', id='bad_deploy'),
        pytest.param('config_diff', id='config_diff'),
        pytest.param('consumer_crash', id='consumer_crash'),
        pytest.param('disk_full', id='disk_full'),
        pytest.param('healthy', id='healthy'),
        pytest.param('lock_contention', id='lock_contention'),
        pytest.param('victim_vs_cause', id='victim_vs_cause'),
    ],
)
def test_run_app_investigation_end_to_end_with_fake_client(
    tmp_path: Path, case_name: str
) -> None:
    """Run app investigation end-to-end on field cases and clean temp files."""
    sess = runtime.create_session_dir(tmp_path / 'sessions')
    case_folder = FIELD_ROOT / 'cases' / case_name
    staged, task = runtime.stage_case_folder(sess, case_folder, suite='field')
    preview = runtime.preview_ingest(staged)
    assert len(preview.catalog.tables) >= 1
    assert len(preview.catalog.documents) >= 1
    assert not preview.catalog.skipped_files
    runs_dir = tmp_path / 'runs'
    seen_types: list[str] = []

    result = runtime.run_app_investigation(
        task,
        use_fake=True,
        runs_dir=runs_dir,
        on_event=lambda ev: seen_types.append(str(ev['type'])),
        cleanup_session_folder=sess,
    )

    assert not sess.exists()
    assert not staged.exists()
    assert result.run_dir is not None
    assert (result.run_dir / 'inputs' / 'c0').is_dir()
    assert 'run_start' in seen_types
    assert 'final_report' in seen_types
    loaded = runtime.load_past_run(result.run_dir / 'trajectory.jsonl')
    vm = view.build_report_view(
        loaded.report,
        loaded.events,
        db_path=loaded.db_path,
        task_fault_start_s=loaded.task_fault_start_s,
    )
    assert vm.run_id == result.run_id
    if case_name == 'healthy':
        assert vm.outcome_text.startswith('No fault found:')
        assert vm.total_citations == 0
    else:
        assert vm.total_citations >= 1
        assert vm.valid_citations == vm.total_citations


@pytest.mark.integration
def test_continue_app_run_after_data_requests_reflects_new_table(
    tmp_path: Path,
) -> None:
    """Continuing a provisional run with a new CSV updates the report."""
    sess = runtime.create_session_dir(tmp_path / 'sessions')
    staged, task = runtime.stage_case_folder(
        sess, scripted.FIELD_CASE, suite='field'
    )
    runs_dir = tmp_path / 'runs'
    verdict_req = {
        'hypothesis_id': 'H1',
        'check': 'Check upstream direction',
    }
    first_client = fake.ScriptedClient(
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.submit(
                'inventory-db',
                data_requests=[
                    {
                        'hypothesis_id': 'H1',
                        'what': 'Deadlock stats table',
                        'source': 'db_stats.csv',
                        'effect': 'Confirms DB lock contention',
                    }
                ],
            ),
            scripted.call(
                graph.VERIFY_NAME, outcome='inconclusive', **verdict_req
            ),
        ]
    )

    first = runtime.run_app_investigation(
        state.Task(folder=staged, case_id=task.case_id),
        runs_dir=runs_dir,
        client_override=first_client,
        cleanup_session_folder=sess,
    )
    assert first.run_dir is not None
    assert first.report.outcome == 'provisional'
    assert len(first.report.data_requests) == 1

    extra = tmp_path / 'db_stats.csv'
    extra.write_text(
        'time,inventory_db_deadlocks\n1700001000,0\n1700001210,14\n',
        encoding='utf-8',
    )
    cont_preview = runtime.preview_continuation(first.run_dir, [extra])
    assert cont_preview.added_files == ('db_stats.csv',)
    assert any('New table db_stats' in line for line in cont_preview.changes)

    second_client = fake.ScriptedClient(
        [
            scripted.call('list_tables'),
            scripted.call(
                'update_hypotheses',
                operations=[
                    {
                        'op': 'revise',
                        'id': 'H1',
                        'evidence': [
                            {
                                'call_id': 'call_0002',
                                'locator': 'db_stats',
                                'quote': 'db_stats.csv',
                            }
                        ],
                    }
                ],
            ),
            scripted.submit('inventory-db'),
            scripted.call('dependency_graph'),
            scripted.call(
                graph.VERIFY_NAME,
                outcome='supported',
                evidence=[
                    {
                        'call_id': 'call_0003',
                        'locator': 'inventory-db',
                        'quote': 'order-service',
                    }
                ],
                **verdict_req,
            ),
        ]
    )
    streamed: list[str] = []
    second = runtime.continue_app_run(
        first.run_dir,
        [extra],
        client_override=second_client,
        on_event=lambda ev: streamed.append(str(ev['type'])),
    )

    assert second.report.continuation == 1
    assert second.report.outcome == 'verified'
    assert len(second.report.causes[0].evidence) == 3
    assert 'continuation' in streamed
    diff = view.build_continuation_diff(
        first.report, second.report, second.events
    )
    assert diff.previous_outcome == 'provisional'
    assert diff.current_outcome == 'verified'
    assert 'call_0002' in diff.new_evidence_calls


@pytest.mark.integration
def test_past_runs_viewer_lists_and_replays_without_model(
    tmp_path: Path,
) -> None:
    """Past-runs viewer skips spend-only logs and replays agent runs."""
    runs_dir = tmp_path / 'runs'
    non_agent = runs_dir / 'check-models-abc'
    non_agent.mkdir(parents=True)
    (non_agent / 'trajectory.jsonl').write_text(
        '{"id": "1", "status": "success"}\n', encoding='utf-8'
    )
    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE, case_id='victim_vs_cause'),
        fake.ScriptedClient(
            [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
                scripted.submit('inventory-db'),
            ]
        ),
        agent_config.AgentConfig(verify=False),
        runs_dir=runs_dir,
    )

    summaries = runtime.list_past_runs(runs_dir)

    assert len(summaries) == 1
    assert summaries[0].run_id == result.run_id
    loaded = runtime.load_past_run(summaries[0].trajectory_path)
    assert loaded.report == result.report
    assert loaded.valid_citations == 1
    assert loaded.total_citations == 1


def test_feedback_round_trip_and_delete_run(tmp_path: Path) -> None:
    """Feedback saves locally and delete_run invokes memory hook + cleanup."""
    runs_root = tmp_path / 'runs'
    run_dir = runs_root / 'run-del'
    run_dir.mkdir(parents=True)
    (run_dir / 'manifest.json').write_text(
        '{"run_id": "run-del"}\n', encoding='utf-8'
    )
    fb = runtime.FeedbackRecord(
        run_id='run-del',
        continuation=0,
        outcome='confirmed',
        actual_root_cause='inventory-db',
        what_fixed_it='Increased connection pool',
        notes='Verified on dashboard',
    )

    saved_path = runtime.save_feedback(run_dir, fb)
    loaded_fb = runtime.load_feedback(run_dir)
    deleted_ids: list[str] = []
    runtime.delete_run(
        run_dir, runs_root=runs_root, memory_deleter=deleted_ids.append
    )

    assert saved_path.name == 'feedback.json'
    assert loaded_fb is not None
    assert loaded_fb.outcome == 'confirmed'
    assert loaded_fb.what_fixed_it == 'Increased connection pool'
    assert deleted_ids == ['run-del']
    assert not run_dir.exists()


def test_delete_run_refuses_path_outside_runs_root(tmp_path: Path) -> None:
    """delete_run refuses deleting directories outside runs_root."""
    runs_root = tmp_path / 'runs'
    runs_root.mkdir()
    outside = tmp_path / 'other'
    outside.mkdir()

    with pytest.raises(ValueError, match='not inside'):
        runtime.delete_run(outside, runs_root=runs_root)


def _scripted_run(runs_dir: Path) -> graph.RunResult:
    return graph.run_investigation(
        state.Task(scripted.FIELD_CASE, case_id='victim_vs_cause'),
        fake.ScriptedClient(
            [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
                scripted.submit('inventory-db'),
            ]
        ),
        agent_config.AgentConfig(verify=False),
        runs_dir=runs_dir,
    )


@pytest.mark.integration
def test_past_runs_hides_eval_cache_and_refuses_to_delete_it(
    tmp_path: Path,
) -> None:
    """Evaluation output shares `runs/` but is never listed or deletable."""
    runs_dir = tmp_path / 'runs'
    app_run = _scripted_run(runs_dir)
    attempts = runs_dir / 'phase-06' / 'real' / 'cache' / 'key' / 'attempts'
    eval_run = _scripted_run(attempts)

    summaries = runtime.list_past_runs(runs_dir)

    assert [item.run_id for item in summaries] == [app_run.run_id]
    assert runtime.is_eval_output(attempts / eval_run.run_id, runs_dir)
    assert not runtime.is_eval_output(runs_dir / app_run.run_id, runs_dir)
    with pytest.raises(ValueError, match='evaluation output'):
        runtime.delete_run(attempts / eval_run.run_id, runs_root=runs_dir)
    assert (attempts / eval_run.run_id).is_dir()


@pytest.mark.integration
def test_past_runs_summary_cache_follows_file_changes(tmp_path: Path) -> None:
    """A rewritten trajectory is re-parsed, an unchanged one is reused."""
    runs_dir = tmp_path / 'runs'
    result = _scripted_run(runs_dir)
    traj = runs_dir / result.run_id / 'trajectory.jsonl'

    first = runtime.list_past_runs(runs_dir)
    again = runtime.list_past_runs(runs_dir)
    traj.write_text('{"id": "1"}\n', encoding='utf-8')
    after = runtime.list_past_runs(runs_dir)

    assert first[0] is again[0]
    assert after == []


class _IngestProbe:
    """Scripted client that records whether ingest was monkeypatched."""

    def __init__(self, inner: fake.ScriptedClient) -> None:
        self.inner = inner
        self.patched: list[bool] = []

    def complete(self, request, *, run, case_id=None):
        self.patched.append(ingest.ingest_folder is not _ORIGINAL_INGEST)
        return self.inner.complete(request, run=run, case_id=case_id)


_ORIGINAL_INGEST = ingest.ingest_folder


@pytest.mark.integration
def test_classification_overrides_are_saved_and_not_patched_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overrides reach ingest as config, and survive for continuations."""
    sess = runtime.create_session_dir(tmp_path / 'sessions')
    staged, task = runtime.stage_case_folder(
        sess, scripted.FIELD_CASE, suite='field'
    )
    probe = _IngestProbe(
        fake.ScriptedClient(
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
    )
    overrides: dict[
        str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
    ] = {'notes.txt': ('log', 'none')}

    result = runtime.run_app_investigation(
        state.Task(folder=staged, case_id=task.case_id),
        runs_dir=tmp_path / 'runs',
        client_override=probe,
        classification_overrides=overrides,
        cleanup_session_folder=sess,
    )

    assert probe.patched and not any(probe.patched)
    assert ingest.ingest_folder is _ORIGINAL_INGEST
    assert result.run_dir is not None
    assert runtime.load_classification_overrides(result.run_dir) == overrides
    (result.run_dir / 'classification_overrides.json').write_text(
        '{"a.txt": ["nonsense", "none"], "b.txt": ["log", "none"]}',
        encoding='utf-8',
    )
    assert runtime.load_classification_overrides(result.run_dir) == {
        'b.txt': ('log', 'none')
    }

    captured: dict[str, object] = {}

    def stub(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(continuation, 'continue_run', stub)
    runtime.continue_app_run(
        result.run_dir, [], use_fake=True, spend_path=tmp_path / 'spend.db'
    )
    config = captured['ingest_config']
    assert isinstance(config, ingest_schema.IngestConfig)
    assert config.classification_overrides == {'b.txt': ('log', 'none')}
