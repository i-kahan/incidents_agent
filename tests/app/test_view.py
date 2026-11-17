"""Unit tests for pure report, live, and diff view models in `app.view`."""

import dataclasses
from pathlib import Path

import pytest

from app import view
from incidents_agent import ingest
from incidents_agent.agent import render
from incidents_agent.agent import report


def _report(
    outcome: report.Outcome = 'verified',
    *,
    stop_reason: str = 'submitted',
    continuation: int = 0,
    causes: tuple[report.RankedCause, ...] = (),
    actions: tuple[report.Action, ...] = (),
    data_requests: tuple[report.DataRequest, ...] = (),
) -> report.Report:
    return report.Report(
        run_id='run-1',
        continuation=continuation,
        outcome=outcome,
        stop_reason=stop_reason,
        fault_type='lock_contention',
        ranks=tuple(cause.component for cause in causes),
        causes=causes,
        actions=actions,
        data_requests=data_requests,
        dropped=(),
        counters={
            'tool_calls': 4,
            'model_turns': 3,
            'cost_usd': '0.0042',
            'elapsed_s': 1.5,
        },
        prompt_hash='hash-abc',
    )


@pytest.mark.parametrize(
    (
        'has_usable',
        'use_fake',
        'exhausted',
        'run_err',
        'stop_reason',
        'outcome',
        'expected',
    ),
    [
        pytest.param(
            False,
            False,
            False,
            None,
            None,
            None,
            'no_usable_model',
            id='no_usable_model',
        ),
        pytest.param(
            True,
            False,
            True,
            None,
            None,
            None,
            'budget_exhausted',
            id='budget_exhausted_pre',
        ),
        pytest.param(
            True,
            False,
            False,
            None,
            'cost_cap',
            'provisional',
            'budget_exhausted',
            id='budget_exhausted_stop',
        ),
        pytest.param(
            True, False, False, 'boom', None, None, 'run_error', id='run_error'
        ),
        pytest.param(
            True,
            False,
            False,
            None,
            'submitted',
            'no_fault_found',
            'no_fault_found',
            id='no_fault_found',
        ),
        pytest.param(
            True,
            False,
            False,
            None,
            'submitted',
            'inconclusive',
            'inconclusive',
            id='inconclusive',
        ),
        pytest.param(
            True,
            False,
            False,
            None,
            'submitted',
            'provisional',
            'provisional',
            id='provisional',
        ),
        pytest.param(
            True,
            False,
            False,
            None,
            'submitted',
            'verified',
            'verified',
            id='verified',
        ),
    ],
)
def test_determine_state_banner_maps_every_state(
    has_usable: bool,
    use_fake: bool,
    exhausted: bool,
    run_err: str | None,
    stop_reason: str | None,
    outcome: report.Outcome | None,
    expected: view.StateKind,
) -> None:
    """Every required app state resolves to a distinct banner kind."""
    final = (
        _report(outcome, stop_reason=stop_reason or 'submitted')
        if outcome is not None
        else None
    )

    banner = view.determine_state_banner(
        has_usable_model=has_usable,
        use_fake=use_fake,
        budget_exhausted=exhausted,
        run_error=run_err,
        final=final,
    )

    assert banner.kind == expected


def test_determine_state_banner_detects_model_error_event() -> None:
    """A trajectory model_error event yields a run_error banner."""
    final = _report('inconclusive', stop_reason='model_error')
    events: list[dict[str, object]] = [
        {'type': 'model_error', 'error': 'ProviderError', 'message': 'timeout'}
    ]

    banner = view.determine_state_banner(
        has_usable_model=True,
        use_fake=False,
        budget_exhausted=False,
        final=final,
        events=events,
    )

    assert banner.kind == 'run_error'
    assert 'timeout' in banner.detail


def test_build_report_view_links_document_chunk_and_heading() -> None:
    """Document evidence exposes its cited chunk text and heading path."""
    cause = report.RankedCause(
        rank=1,
        component='auth-service',
        statement='Timeout lowered in deploy diff',
        hypothesis_id='H1',
        verification='supported',
        confidence='high',
        confidence_reasons=('1 valid citation',),
        evidence=(
            report.EvidenceRef(
                'call_0001',
                'deploy_diff.patch::0',
                'AUTH_JWKS_TIMEOUT_MS: 5',
            ),
        ),
    )
    events: list[dict[str, object]] = [
        {
            'type': 'tool_call',
            'seq': 1,
            'call_id': 'call_0001',
            'run_id': 'run-1',
            'continuation': 0,
            'role': 'agent',
            'node': 'investigate',
            'tool': 'read_document',
            'args': {'chunk_id': 'deploy_diff.patch::0'},
            'ok': True,
            'locator': {
                'kind': 'document_chunks',
                'table': 'document_chunks',
                'chunk_ids': ['deploy_diff.patch::0'],
            },
            'result': {
                'ok': True,
                'untrusted_data': {
                    '_untrusted_source': True,
                    'notice': 'Untrusted data',
                    'content': {
                        'chunks': [
                            {
                                'chunk_id': 'deploy_diff.patch::0',
                                'doc_id': 'deploy_diff.patch',
                                'position': 0,
                                'heading_path': (
                                    'deploy/auth-service.yaml > @@ -18,3 @@'
                                ),
                                'text': '+  AUTH_JWKS_TIMEOUT_MS: 5',
                                'is_target': True,
                            }
                        ]
                    },
                },
                'locator': {
                    'kind': 'document_chunks',
                    'table': 'document_chunks',
                    'chunk_ids': ['deploy_diff.patch::0'],
                },
            },
        }
    ]
    final = _report('verified', causes=(cause,))

    vm = view.build_report_view(final, events)

    ev = vm.causes[0].evidence[0]
    assert ev.valid is True
    assert ev.tool == 'read_document'
    assert ev.document_chunk is not None
    assert ev.document_chunk.heading_path == (
        'deploy/auth-service.yaml > @@ -18,3 @@'
    )
    assert 'AUTH_JWKS_TIMEOUT_MS: 5' in ev.document_chunk.text
    assert vm.markdown == render.render(final, {'call_0001': 'read_document'})


def test_build_report_view_orders_read_only_actions_first() -> None:
    """Recommended actions sort read-only checks before disruptive ones."""
    final = _report(
        'verified',
        actions=(
            report.Action('H1', 'Restart service', 'disruptive', 'Errors drop'),
            report.Action('H1', 'Revert flag', 'reversible', 'Config matches'),
            report.Action('H1', 'Inspect locks', 'read_only', 'Count is 0'),
        ),
    )

    vm = view.build_report_view(final, [])

    assert [item.risk for item in vm.actions] == [
        'read_only',
        'reversible',
        'disruptive',
    ]
    assert vm.actions[0].risk_label == 'Read-only check'


def test_build_report_view_pairs_data_requests_with_hypotheses() -> None:
    """Data requests include the component and statement from the ledger."""
    final = _report(
        'provisional',
        data_requests=(
            report.DataRequest(
                'H2',
                'Connection pool metrics',
                'Prometheus',
                'Confirms saturation if active == max',
            ),
        ),
    )
    events: list[dict[str, object]] = [
        {
            'type': 'ledger',
            'ledger': {
                'run_id': 'run-1',
                'entries': [
                    {
                        'id': 'H2',
                        'component': 'inventory-db',
                        'statement': 'DB pool exhausted',
                        'status': 'open',
                        'confidence': 'medium',
                        'verification': 'inconclusive',
                    }
                ],
            },
        }
    ]

    vm = view.build_report_view(final, events)

    assert len(vm.data_requests) == 1
    assert vm.data_requests[0].component == 'inventory-db'
    assert vm.data_requests[0].hypothesis_statement == 'DB pool exhausted'


def test_build_report_view_extracts_charts_from_duckdb_and_time_series(
    tmp_path: Path,
) -> None:
    """Metric citations render full time series when DuckDB is present."""
    case_dir = tmp_path / 'case'
    case_dir.mkdir()
    (case_dir / 'metrics.csv').write_text(
        'time,cpu\n'
        '1700001000,10.0\n'
        '1700001010,12.0\n'
        '1700001020,85.0\n'
        '1700001030,90.0\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(case_dir, workdir=tmp_path / 'db')
    db_path = ingested.db_path
    ingested.close()

    cause = report.RankedCause(
        rank=1,
        component='web',
        statement='CPU spiked',
        hypothesis_id='H1',
        verification='supported',
        confidence='high',
        confidence_reasons=('1 valid citation',),
        evidence=(report.EvidenceRef('call_0001', 'metrics.cpu', '90.0'),),
    )
    events: list[dict[str, object]] = [
        {
            'type': 'tool_call',
            'seq': 1,
            'call_id': 'call_0001',
            'run_id': 'run-1',
            'continuation': 0,
            'role': 'agent',
            'node': 'investigate',
            'tool': 'compare_windows',
            'args': {'table': 'metrics', 'split_time': 1700001015.0},
            'ok': True,
            'locator': {
                'kind': 'window_comparison',
                'table': 'metrics',
                'columns': ['cpu'],
            },
            'result': {
                'ok': True,
                'table': 'metrics',
                'split_time': 1700001015.0,
                'ranked': [
                    {
                        'column': 'cpu',
                        'score': 12.0,
                        'before': {'median': 11.0},
                        'after': {'median': 90.0},
                    }
                ],
                'locator': {
                    'kind': 'window_comparison',
                    'table': 'metrics',
                    'columns': ['cpu'],
                },
            },
        }
    ]
    final = _report('verified', causes=(cause,))

    vm_with_db = view.build_report_view(final, events, db_path=db_path)
    vm_no_db = view.build_report_view(final, events, db_path=None)

    assert len(vm_with_db.charts) == 1
    assert vm_with_db.charts[0].kind == 'time_series'
    assert vm_with_db.charts[0].split_time_s == 1700001015.0
    assert len(vm_with_db.charts[0].points) >= 3
    assert len(vm_no_db.charts) == 1
    assert vm_no_db.charts[0].kind == 'window_comparison'
    assert len(vm_no_db.charts[0].points) == 2


def test_build_report_view_extracts_estimated_onset_when_omitted() -> None:
    """When fault_start_s was omitted, `find_onset` estimate is surfaced."""
    events: list[dict[str, object]] = [
        {
            'type': 'tool_call',
            'seq': 1,
            'call_id': 'call_0001',
            'run_id': 'run-1',
            'continuation': 0,
            'role': 'agent',
            'node': 'investigate',
            'tool': 'find_onset',
            'args': {},
            'ok': True,
            'result': {
                'ok': True,
                'onset_detected': True,
                'estimated_onset_time_s': 1700001200.0,
                'confidence': 'high',
                'signals': [
                    {
                        'table': 'metrics',
                        'column': 'cpu',
                        'signal_type': 'metric_shift',
                        'peak_z_score': 9.4,
                    }
                ],
            },
        }
    ]

    vm_omitted = view.build_report_view(_report('provisional'), events)
    vm_supplied = view.build_report_view(
        _report('provisional'), events, task_fault_start_s=1700001190.0
    )

    assert vm_omitted.estimated_onset is not None
    assert vm_omitted.estimated_onset.estimated_onset_time_s == 1700001200.0
    assert vm_omitted.estimated_onset.confidence == 'high'
    assert vm_supplied.estimated_onset is None


def test_build_live_view_groups_calls_and_tracks_ledger() -> None:
    """Live view aggregates active node, counters, lanes, and ledger."""
    events: list[dict[str, object]] = [
        {'type': 'node', 'node': 'investigate', 'role': 'agent'},
        {
            'type': 'model_call',
            'role': 'agent',
            'budget': {
                'tool_calls': 1,
                'model_turns': 1,
                'cost_usd': '0.0012',
                'elapsed_s': 0.8,
            },
        },
        {
            'type': 'tool_call',
            'seq': 3,
            'call_id': 'call_0001',
            'continuation': 0,
            'role': 'agent',
            'node': 'investigate',
            'tool': 'list_tables',
            'args': {},
            'ok': True,
            'locator': {'kind': 'catalog'},
            'target_components': [],
        },
        {
            'type': 'ledger',
            'role': 'agent',
            'ledger': {
                'run_id': 'run-1',
                'entries': [
                    {
                        'id': 'H1',
                        'component': 'db',
                        'statement': 'Locks spiked',
                        'status': 'supported',
                        'confidence': 'medium',
                        'verification': 'unverified',
                        'evidence_for': [{'call_id': 'call_0001'}],
                        'evidence_against': [],
                        'origin': 'data',
                    }
                ],
            },
        },
    ]

    live = view.build_live_view(events)

    assert live.active_node == 'investigate'
    assert live.cost_usd == '0.0012'
    assert live.model_turns == 1
    assert 'agent' in live.lanes
    assert live.lanes['agent'].tool_calls[0].tool == 'list_tables'
    assert live.hypotheses[0].id == 'H1'
    assert live.hypotheses[0].evidence_for == 1


def test_build_continuation_diff_summarises_changes() -> None:
    """Continuation diff reports added files, rank shifts, and new evidence."""
    prev_cause = report.RankedCause(
        1,
        'api-gateway',
        'Queue inflated',
        'H1',
        'inconclusive',
        'medium',
        ('1 citation',),
        (report.EvidenceRef('call_0001', 'metrics', '18'),),
    )
    curr_cause = report.RankedCause(
        1,
        'inventory-db',
        'DB locks stalled callers',
        'H2',
        'supported',
        'high',
        ('verified',),
        (
            report.EvidenceRef('call_0001', 'metrics', '18'),
            report.EvidenceRef('call_0002', 'db_stats', '14'),
        ),
    )
    prev = _report('provisional', continuation=0, causes=(prev_cause,))
    curr = dataclasses.replace(
        _report('verified', continuation=1, causes=(curr_cause,)),
        ranks=('inventory-db',),
    )
    events: list[dict[str, object]] = [
        {
            'type': 'continuation',
            'continuation': 1,
            'added': ['db_stats.csv'],
            'changes': ['- New table db_stats from db_stats.csv: 2 rows'],
        }
    ]

    diff = view.build_continuation_diff(prev, curr, events)

    assert diff.added_files == ('db_stats.csv',)
    assert diff.previous_outcome == 'provisional'
    assert diff.current_outcome == 'verified'
    assert diff.previous_ranks == ('api-gateway',)
    assert diff.current_ranks == ('inventory-db',)
    assert diff.new_evidence_calls == ('call_0002',)
    assert 'inventory-db: new (supported)' in diff.verification_changes


def test_build_report_view_extracts_search_documents_excerpt() -> None:
    """`search_documents` evidence exposes chunk excerpt and heading."""
    cause = report.RankedCause(
        1,
        'auth-service',
        'Timeout lowered',
        'H1',
        'supported',
        'high',
        ('1 valid citation',),
        (
            report.EvidenceRef(
                'call_0001',
                'runbook.md::1',
                'AUTH_JWKS_TIMEOUT_MS',
            ),
        ),
    )
    events: list[dict[str, object]] = [
        {
            'type': 'tool_call',
            'seq': 1,
            'call_id': 'call_0001',
            'run_id': 'run-1',
            'continuation': 0,
            'role': 'agent',
            'node': 'investigate',
            'tool': 'search_documents',
            'args': {'query': 'AUTH_JWKS_TIMEOUT_MS'},
            'ok': True,
            'locator': {
                'kind': 'document_chunks',
                'table': 'document_chunks',
                'chunk_ids': ['runbook.md::1'],
            },
            'result': {
                'ok': True,
                'untrusted_data': {
                    '_untrusted_source': True,
                    'notice': 'Untrusted',
                    'content': {
                        'results': [
                            {
                                'chunk_id': 'runbook.md::1',
                                'doc_id': 'runbook.md',
                                'heading_path': 'Auth > Timeouts',
                                'excerpt': 'Check AUTH_JWKS_TIMEOUT_MS >= 500',
                            }
                        ]
                    },
                },
                'locator': {
                    'kind': 'document_chunks',
                    'table': 'document_chunks',
                    'chunk_ids': ['runbook.md::1'],
                },
            },
        }
    ]

    vm = view.build_report_view(_report('verified', causes=(cause,)), events)

    chunk = vm.causes[0].evidence[0].document_chunk
    assert chunk is not None
    assert chunk.heading_path == 'Auth > Timeouts'
    assert 'AUTH_JWKS_TIMEOUT_MS' in chunk.text


def test_build_report_view_extracts_time_series_chart() -> None:
    """`time_series` citations populate plottable MetricChart points."""
    cause = report.RankedCause(
        1,
        'web',
        'Latency spiked',
        'H1',
        'supported',
        'high',
        ('1 citation',),
        (report.EvidenceRef('call_0001', 'metrics', '120.5'),),
    )
    events: list[dict[str, object]] = [
        {
            'type': 'tool_call',
            'seq': 1,
            'call_id': 'call_0001',
            'run_id': 'run-1',
            'continuation': 0,
            'role': 'agent',
            'node': 'investigate',
            'tool': 'time_series',
            'args': {'table': 'metrics', 'column': 'latency'},
            'ok': True,
            'locator': {
                'kind': 'time_series',
                'table': 'metrics',
                'columns': ['latency'],
            },
            'result': {
                'ok': True,
                'table': 'metrics',
                'column': 'latency',
                'buckets': [
                    {'bucket_start': 1700001000.0, 'value': 12.0},
                    {'bucket_start': 1700001060.0, 'value': 120.5},
                ],
                'locator': {
                    'kind': 'time_series',
                    'table': 'metrics',
                    'columns': ['latency'],
                },
            },
        }
    ]

    vm = view.build_report_view(_report('verified', causes=(cause,)), events)

    assert len(vm.charts) == 1
    assert vm.charts[0].column == 'latency'
    assert [pt.value for pt in vm.charts[0].points] == [12.0, 120.5]


def test_build_live_view_orders_multi_agent_role_lanes_and_splits_testers() -> (
    None
):
    """Multi-agent role lanes sort lead -> testers (H1..Hn) -> skeptic."""
    events: list[dict[str, object]] = [
        {'type': 'node', 'node': 'skeptic', 'role': 'skeptic'},
        {
            'type': 'model_call',
            'role': 'tester',
            'hypothesis_id': 'H2',
            'budget': {
                'tool_calls': 1,
                'model_turns': 2,
                'cost_usd': '0.0020',
                'elapsed_s': 1.2,
            },
        },
        {
            'type': 'tool_call',
            'seq': 2,
            'call_id': 'call_0002',
            'continuation': 0,
            'role': 'tester',
            'hypothesis_id': 'H2',
            'node': 'testers',
            'tool': 'run_sql',
            'args': {'query': 'SELECT 1'},
            'ok': True,
        },
        {'type': 'model_call', 'role': 'lead', 'budget': {'tool_calls': 1}},
        {
            'type': 'tool_call',
            'seq': 1,
            'call_id': 'call_0001',
            'continuation': 0,
            'role': 'lead',
            'node': 'lead',
            'tool': 'list_tables',
            'args': {},
            'ok': True,
        },
        {'type': 'ledger', 'role': 'lead', 'ledger': {'entries': []}},
        {
            'type': 'tool_call',
            'seq': 3,
            'call_id': 'call_0003',
            'continuation': 0,
            'role': 'tester',
            'hypothesis_id': 'H1',
            'node': 'testers',
            'tool': 'search_text',
            'args': {'pattern': 'timeout'},
            'ok': True,
        },
        {
            'type': 'model_call',
            'role': 'tester',
            'budget': {
                'role': 'tester',
                'hypothesis_id': 'H3',
                'run': {
                    'tool_calls': 4,
                    'model_turns': 5,
                    'cost_usd': '0.0040',
                    'elapsed_s': 2.5,
                },
            },
        },
        {
            'type': 'verification',
            'role': 'skeptic',
            'outcome': 'supported',
            'hypothesis_id': 'H1',
        },
    ]

    live = view.build_live_view(events)

    assert list(live.lanes.keys()) == [
        'lead',
        'tester:H1',
        'tester:H2',
        'tester:H3',
        'skeptic',
    ]
    assert live.lanes['lead'].label == 'Lead investigator'
    assert live.lanes['lead'].ledger_updates == 1
    assert live.lanes['tester:H1'].hypothesis_id == 'H1'
    assert live.lanes['tester:H1'].label == 'Tester (H1)'
    assert live.lanes['tester:H1'].tool_calls[0].hypothesis_id == 'H1'
    assert live.lanes['tester:H3'].role == 'tester'
    assert live.lanes['tester:H3'].hypothesis_id == 'H3'
    assert live.lanes['skeptic'].label == 'Independent skeptic'
    assert live.lanes['skeptic'].verification_outcome == 'supported'
    assert live.tool_calls >= 4
    assert live.cost_usd == '0.0040'
    assert live.elapsed_s == 2.5


def test_build_continuation_diff_records_hypothesis_changes() -> None:
    """Continuation diff tracks confidence, rank, and membership shifts."""
    prev = dataclasses.replace(
        _report(
            'provisional',
            continuation=0,
            causes=(
                report.RankedCause(
                    1,
                    'api-gateway',
                    'Gateway timed out',
                    'H1',
                    'inconclusive',
                    'low',
                    ('1 citation',),
                    (report.EvidenceRef('call_0001', 'metrics', '18'),),
                ),
                report.RankedCause(
                    2,
                    'cache',
                    'Cache miss spike',
                    'H2',
                    'unverified',
                    'low',
                    ('1 citation',),
                    (report.EvidenceRef('call_0001', 'metrics', '9'),),
                ),
            ),
        ),
        ranks=('api-gateway', 'cache'),
    )
    curr = dataclasses.replace(
        _report(
            'verified',
            continuation=1,
            causes=(
                report.RankedCause(
                    1,
                    'inventory-db',
                    'DB lock contention',
                    'H3',
                    'supported',
                    'high',
                    ('verified',),
                    (report.EvidenceRef('call_0002', 'db', '14'),),
                ),
                report.RankedCause(
                    2,
                    'api-gateway',
                    'Gateway timed out',
                    'H1',
                    'inconclusive',
                    'medium',
                    ('2 citations',),
                    (report.EvidenceRef('call_0001', 'metrics', '18'),),
                ),
            ),
        ),
        ranks=('inventory-db', 'api-gateway'),
    )

    diff = view.build_continuation_diff(prev, curr, [])

    assert 'inventory-db: newly ranked #1 (high)' in diff.hypothesis_changes
    assert (
        'api-gateway: rank #1 -> #2, confidence low -> medium'
        in diff.hypothesis_changes
    )
    assert 'cache: dropped from ranking (was #2)' in diff.hypothesis_changes
