"""Unit and integration tests for folder ingest, limits, and read-only DB."""

import os
import pathlib

import duckdb
import pytest

from incidents_agent import ingest
from incidents_agent.ingest import schema


def _re2_ob_case_path() -> pathlib.Path | None:
    env_override = os.environ.get('INCIDENTS_RE2_OB_CASE')
    if env_override:
        candidate = pathlib.Path(env_override)
        if candidate.is_dir():
            return candidate
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    local = repo_root / 'agent_tasks' / 're2-ob' / 'cases' / 're2-ob-0001'
    return local if local.is_dir() else None


def test_ingest_loads_csv_parquet_jsonl_logs_and_documents(
    tmp_path: pathlib.Path,
) -> None:
    """Folder ingest loads all supported tabular, log, and document formats."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text(
        'time,api_cpu\n1700000000,10.5\n1700000060,88.2\n',
        encoding='utf-8',
    )
    (src / 'events.jsonl').write_text(
        '{"timestamp": "2024-01-01T00:00:00Z", "svc": "api"}\n',
        encoding='utf-8',
    )
    (src / 'app.log').write_text(
        '2024-01-01T00:00:01Z ERROR api Timeout\n'
        '2024-01-01T00:00:02Z INFO api Recovered\n',
        encoding='utf-8',
    )
    (src / 'runbook.md').write_text(
        '# API Runbook\n\n## Timeouts\n\nCheck pool limits.\n',
        encoding='utf-8',
    )
    pq_path = src / 'spans.parquet'
    writer = duckdb.connect(':memory:')
    writer.execute(
        f'COPY (SELECT 1700000000 AS time, 42.0 AS dur) '
        f"TO '{pq_path}' (FORMAT PARQUET)"
    )
    writer.close()

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        table_names = {entry.name for entry in result.catalog.tables}
        doc_ids = {doc.doc_id for doc in result.catalog.documents}
        chunk_rows = result.connection.execute(
            'SELECT doc_id, chunk_id FROM document_chunks'
        ).fetchall()

        metrics = result.catalog.get_table('metrics')
        app_tbl = result.catalog.get_table('app')

        assert table_names == {'metrics', 'events', 'app', 'spans'}
        assert doc_ids == {'runbook.md'}
        assert len(chunk_rows) == 1
        assert metrics is not None
        assert len(metrics.sample_rows) == 2
        assert app_tbl is not None
        assert app_tbl.time_info.time_usable is True
    finally:
        result.close()


def test_ingest_disambiguates_same_filename_in_different_folders(
    tmp_path: pathlib.Path,
) -> None:
    """Two metrics.csv files in different folders produce unique table names."""
    src = tmp_path / 'case'
    (src / 'zone_a').mkdir(parents=True)
    (src / 'zone_b').mkdir(parents=True)
    (src / 'metrics.csv').write_text('time,v\n1700000000,1\n', encoding='utf-8')
    (src / 'zone_a' / 'metrics.csv').write_text(
        'time,v\n1700000000,2\n', encoding='utf-8'
    )
    (src / 'zone_b' / 'metrics.csv').write_text(
        'time,v\n1700000000,3\n', encoding='utf-8'
    )

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        by_file = {
            entry.source_file: entry.name for entry in result.catalog.tables
        }

        assert by_file == {
            'metrics.csv': 'metrics',
            'zone_a/metrics.csv': 'zone_a__metrics',
            'zone_b/metrics.csv': 'zone_b__metrics',
        }
        assert len(set(by_file.values())) == 3
    finally:
        result.close()


def test_ingest_enforces_limits_and_continues_after_bad_files(
    tmp_path: pathlib.Path,
) -> None:
    """Oversized, corrupt, and over-count files are skipped without aborting."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'a_big.csv').write_text(
        'time,v\n1700000000,' + ('9' * 200) + '\n', encoding='utf-8'
    )
    (src / 'b_binary.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    (src / 'c_corrupt.jsonl').write_text('{invalid json', encoding='utf-8')
    (src / 'd_good1.csv').write_text('time,v\n1700000000,1\n', encoding='utf-8')
    (src / 'e_good2.csv').write_text('time,v\n1700000001,2\n', encoding='utf-8')
    (src / 'f_extra.csv').write_text('time,v\n1700000002,3\n', encoding='utf-8')

    cfg = schema.IngestConfig(
        limits=schema.IngestLimits(
            max_file_bytes=120,
            max_files=2,
            max_total_bytes=200,
        )
    )
    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        loaded = [t.source_file for t in result.catalog.tables]
        skipped = {
            s.relative_path: s.reason for s in result.catalog.skipped_files
        }

        assert loaded == ['d_good1.csv', 'e_good2.csv']
        assert 'max_file_bytes' in result.catalog.limits_hit
        assert 'max_files' in result.catalog.limits_hit
        assert 'a_big.csv' in skipped
        assert 'b_binary.png' in skipped
        assert 'c_corrupt.jsonl' in skipped
        assert 'f_extra.csv' in skipped
    finally:
        result.close()


def test_ingest_enforces_total_byte_limit(tmp_path: pathlib.Path) -> None:
    """Files exceeding remaining max_total_bytes are skipped and recorded."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'a.txt').write_text('first file\n' * 5, encoding='utf-8')
    (src / 'b.txt').write_text('second file\n' * 10, encoding='utf-8')

    cfg = schema.IngestConfig(
        limits=schema.IngestLimits(max_file_bytes=500, max_total_bytes=70)
    )
    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        assert [d.source_file for d in result.catalog.documents] == ['a.txt']
        assert 'max_total_bytes' in result.catalog.limits_hit
        assert any(
            s.relative_path == 'b.txt' for s in result.catalog.skipped_files
        )
    finally:
        result.close()


def test_ingest_handles_empty_table_without_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """Empty CSV with header and empty log file ingest as zero-row tables."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text('time,cpu\n', encoding='utf-8')
    (src / 'empty.log').write_text('', encoding='utf-8')

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        metrics = result.catalog.get_table('metrics')
        empty_log = result.catalog.get_table('empty')

        assert metrics is not None
        assert metrics.row_count == 0
        assert metrics.time_info.time_usable is False
        assert empty_log is not None
        assert empty_log.row_count == 0
    finally:
        result.close()


@pytest.mark.integration
def test_ingest_connection_is_read_only_and_disables_external_access(
    tmp_path: pathlib.Path,
) -> None:
    """Returned DuckDB connection blocks writes and external file reads."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text(
        'time,cpu\n1700000000,1\n', encoding='utf-8'
    )
    secret_csv = tmp_path / 'outside.csv'
    secret_csv.write_text('secret\npassword123\n', encoding='utf-8')

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        with pytest.raises(duckdb.Error):
            result.connection.execute('INSERT INTO metrics VALUES (1, 2, 1.0)')
        with pytest.raises(duckdb.Error):
            result.connection.execute(
                f"SELECT * FROM read_csv_auto('{secret_csv}')"
            ).fetchall()
    finally:
        result.close()


def test_ingest_missing_folder_raises_file_not_found(
    tmp_path: pathlib.Path,
) -> None:
    """Ingesting a non-existent folder fails with FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        ingest.ingest_folder(tmp_path / 'does_not_exist')


@pytest.mark.integration
def test_ingest_real_re2_ob_case(tmp_path: pathlib.Path) -> None:
    """Ingest one real exported RE2-OB case when available locally."""
    case_dir = _re2_ob_case_path()
    if case_dir is None:
        pytest.skip('RE2-OB blind export not present locally')

    result = ingest.ingest_folder(case_dir, workdir=tmp_path / 're2-db')
    try:
        metrics = result.catalog.get_table('metrics')
        logs = result.catalog.get_table('logs')
        traces = result.catalog.get_table('traces')

        assert result.catalog.skipped_files == ()
        assert metrics is not None
        assert metrics.time_info.column == 'time'
        assert metrics.time_info.unit == 'unix_s'
        assert metrics.time_info.time_usable is True
        assert logs is not None
        assert logs.time_info.column == 'timestamp'
        assert logs.time_info.unit == 'unix_ns'
        assert logs.time_info.time_usable is True
        assert traces is not None
        assert traces.time_info.column in ('startTimeMillis', 'startTime')
        assert traces.time_info.time_usable is True
        assert len(traces.relationships) == 1
        assert traces.relationships[0].kind == 'span_parent'
        assert traces.relationships[0].overlap_ratio == 1.0
    finally:
        result.close()
