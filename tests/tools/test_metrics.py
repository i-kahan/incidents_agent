"""Unit tests for compare_windows, time_series, and find_onset."""

import csv
import pathlib

import pytest

from incidents_agent import ingest
from incidents_agent import tools
from tests.tools import conftest


def _write_metrics_fixture(folder: pathlib.Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'metrics.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['time', 'checkout_cpu', 'frontend_cpu', 'stable_mem'])
        for i in range(40):
            ts = 1_700_000_000 + i * 10
            checkout = 12.0 + (i % 3) * 0.5 if ts <= 1_700_000_200 else 92.0
            frontend = 15.0 if ts <= 1_700_000_200 else 22.0
            writer.writerow([ts, checkout, frontend, 1024.0])


def test_compare_windows_ranks_shifted_column_first(
    tmp_path: pathlib.Path,
) -> None:
    """compare_windows places the column with largest shift at rank 0."""
    src = tmp_path / 'case'
    _write_metrics_fixture(src)
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'compare_windows',
            {'table': 'metrics', 'split_time': 1_700_000_200, 'top_k': 3},
        )

        assert res['ok'] is True
        ranked = conftest.as_dict_list(res['ranked'])
        assert ranked[0]['column'] == 'checkout_cpu'
        assert ranked[0]['direction'] == 'up'
        assert float(str(ranked[0]['score'])) > float(str(ranked[1]['score']))
        assert conftest.as_dict(res['locator'])['kind'] == 'window_comparison'
    finally:
        ingested.close()


def test_compare_windows_refuses_empty_window_and_unusable_time(
    tmp_path: pathlib.Path,
) -> None:
    """compare_windows returns explicit errors on empty window or bad time."""
    src = tmp_path / 'case'
    _write_metrics_fixture(src)
    (src / 'notime.csv').write_text(
        'time,val\n20:35,1\n20:36,9\n', encoding='utf-8'
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        empty_win = reg.call(
            'compare_windows',
            {'table': 'metrics', 'split_time': 1_600_000_000},
        )
        bad_time = reg.call(
            'compare_windows',
            {'table': 'notime', 'split_time': 1_700_000_200},
        )

        assert empty_win['ok'] is False
        assert conftest.as_dict(empty_win['error'])['code'] == 'empty_window'
        assert bad_time['ok'] is False
        assert conftest.as_dict(bad_time['error'])['code'] == 'unusable_time'
    finally:
        ingested.close()


def test_time_series_downsamples_into_bounded_buckets(
    tmp_path: pathlib.Path,
) -> None:
    """time_series returns at most num_buckets aggregated points."""
    src = tmp_path / 'case'
    _write_metrics_fixture(src)
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'time_series',
            {'table': 'metrics', 'column': 'checkout_cpu', 'num_buckets': 8},
        )

        assert res['ok'] is True
        buckets = conftest.as_dict_list(res['buckets'])
        assert 1 <= len(buckets) <= 8
        assert buckets[-1]['max'] == pytest.approx(92.0)
        assert float(str(buckets[0]['avg'])) < 20.0
    finally:
        ingested.close()


def test_find_onset_estimates_shift_timestamp_and_detects_flat_baseline(
    tmp_path: pathlib.Path,
) -> None:
    """find_onset locates the incident change point within 30 seconds."""
    src = tmp_path / 'case'
    _write_metrics_fixture(src)
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('find_onset', {'num_buckets': 40})

        assert res['ok'] is True
        assert res['onset_detected'] is True
        assert (
            abs(float(str(res['estimated_onset_time_s'])) - 1_700_000_200)
            <= 30.0
        )
    finally:
        ingested.close()


def test_find_onset_reports_no_onset_on_healthy_table(
    tmp_path: pathlib.Path,
) -> None:
    """Flat healthy metrics report onset_detected=False instead of guessing."""
    src = tmp_path / 'case'
    src.mkdir()
    rows = '\n'.join(f'{1_700_000_000 + i * 10},10.0' for i in range(20))
    (src / 'metrics.csv').write_text(f'time,cpu\n{rows}\n', encoding='utf-8')
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('find_onset', {'num_buckets': 20})

        assert res['ok'] is True
        assert res['onset_detected'] is False
        assert res['estimated_onset_time_s'] is None
    finally:
        ingested.close()


def test_find_onset_skips_struct_columns_containing_int_substring(
    tmp_path: pathlib.Path,
) -> None:
    """Nested STRUCT columns (even with 'container' fields) are not numeric."""
    src = tmp_path / 'case'
    src.mkdir()
    lines = [
        (
            f'{{"time": {1_700_000_000 + i * 10}, '
            f'"labels": {{"container": "web", "point": "a"}}, '
            f'"value": {10.0 if i < 10 else 90.0}}}'
        )
        for i in range(20)
    ]
    (src / 'alerts.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('find_onset', {'num_buckets': 20})

        assert res['ok'] is True
        assert res['onset_detected'] is True
        signals = res['signals']
        assert isinstance(signals, list)
        cols = [sig['column'] for sig in signals]
        assert 'value' in cols
        assert 'labels' not in cols
    finally:
        ingested.close()


def test_long_format_metric_name_table_groups_by_series(
    tmp_path: pathlib.Path,
) -> None:
    """Long-format (metric_name, value) tables rank and detect per series."""
    src = tmp_path / 'case'
    src.mkdir()
    rows: list[str] = []
    for i in range(20):
        ts = 1_700_000_000 + i * 10
        steady = 10.0 + (i % 2) * 0.1
        spiked = 1.0 + (i % 2) * 0.1 if i < 10 else 100.0 + (i % 2) * 0.1
        rows.append(f'{ts},steady_metric,{steady}')
        rows.append(f'{ts},spiked_metric,{spiked}')
    (src / 'pod_metrics.tsv').write_text(
        'time\tmetric_name\tvalue\n'
        + '\n'.join(rows).replace(',', '\t')
        + '\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        onset = reg.call('find_onset', {'num_buckets': 20})
        assert onset['ok'] is True
        assert onset['onset_detected'] is True
        signals = onset['signals']
        assert isinstance(signals, list)
        assert signals[0]['column'] == 'spiked_metric'

        cmp_res = reg.call(
            'compare_windows',
            {'table': 'pod_metrics', 'split_time': 1_700_000_095},
        )
        assert cmp_res['ok'] is True
        assert cmp_res['group_by'] == 'metric_name'
        ranked = cmp_res['ranked']
        assert isinstance(ranked, list)
        assert ranked[0]['series'] == 'spiked_metric'
        assert ranked[0]['direction'] == 'up'
    finally:
        ingested.close()


def test_find_onset_expands_glob_table_patterns(
    tmp_path: pathlib.Path,
) -> None:
    """find_onset expands wildcard patterns like metrics__* cleanly."""
    src = tmp_path / 'case'
    _write_metrics_fixture(src)
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('find_onset', {'tables': ['met*'], 'num_buckets': 40})

        assert res['ok'] is True
        assert res['onset_detected'] is True
    finally:
        ingested.close()
