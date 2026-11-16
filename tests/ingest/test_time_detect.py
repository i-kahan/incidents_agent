"""Unit tests for timestamp detection and _time_unix_s normalisation."""

import duckdb
import pytest

from incidents_agent.ingest import schema
from incidents_agent.ingest import time_detect


def _columns_for(
    conn: duckdb.DuckDBPyConnection, table_name: str
) -> tuple[schema.ColumnInfo, ...]:
    rows = conn.execute(f'DESCRIBE {table_name}').fetchall()
    return tuple(
        schema.ColumnInfo(name=str(row[0]), dtype=str(row[1])) for row in rows
    )


@pytest.mark.parametrize(
    ('value_expr', 'expected_unit', 'expected_unix_s'),
    [
        pytest.param('1705696521', 'unix_s', 1705696521.0, id='unix_s'),
        pytest.param('1705696521129', 'unix_ms', 1705696521.129, id='unix_ms'),
        pytest.param(
            '1705696521129626', 'unix_us', 1705696521.129626, id='unix_us'
        ),
        pytest.param(
            '1705696521129621311',
            'unix_ns',
            1705696521.1296213,
            id='unix_ns',
        ),
        pytest.param(
            "'2024-01-19T20:35:21Z'", 'iso8601', 1705696521.0, id='iso_utc'
        ),
        pytest.param(
            "'2024-01-19T22:35:21+02:00'",
            'iso8601',
            1705696521.0,
            id='iso_offset',
        ),
        pytest.param(
            "'2024-01-19 20:35:21'", 'iso8601', 1705696521.0, id='iso_naive_utc'
        ),
    ],
)
def test_detect_and_normalise_time_infers_units_and_keeps_original(
    value_expr: str,
    expected_unit: schema.TimeUnit,
    expected_unix_s: float,
) -> None:
    """Epoch units and ISO-8601 timestamps normalise into _time_unix_s."""
    conn = duckdb.connect(':memory:')
    conn.execute(f'CREATE TABLE events AS SELECT {value_expr} AS ts, 1 AS val')
    cols = _columns_for(conn, 'events')

    info = time_detect.detect_and_normalise_time(conn, 'events', cols)
    row = conn.execute('SELECT ts, _time_unix_s FROM events').fetchone()

    assert info.time_usable is True
    assert info.column == 'ts'
    assert info.normalised_column == '_time_unix_s'
    assert info.unit == expected_unit
    assert row is not None
    assert row[1] == pytest.approx(expected_unix_s, rel=1e-6)
    conn.close()


def test_detect_and_normalise_time_prefers_ns_timestamp_over_hhmm() -> None:
    """High-precision timestamp wins over HH:MM clock column like RE2-OB."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        'CREATE TABLE logs AS '
        "SELECT '20:35' AS time, 1705696521129621311 AS timestamp, "
        "'started' AS message"
    )
    cols = _columns_for(conn, 'logs')

    info = time_detect.detect_and_normalise_time(conn, 'logs', cols)

    assert info.time_usable is True
    assert info.column == 'timestamp'
    assert info.unit == 'unix_ns'
    assert info.min_time_s == pytest.approx(1705696521.1296213, rel=1e-6)
    conn.close()


def test_detect_and_normalise_time_flags_clock_only_table_unusable() -> None:
    """A table with only HH:MM clock strings is marked time_usable=False."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        "CREATE TABLE clock_only AS SELECT '20:35' AS time, 10.0 AS val"
    )
    cols = _columns_for(conn, 'clock_only')

    info = time_detect.detect_and_normalise_time(conn, 'clock_only', cols)
    desc = conn.execute('DESCRIBE clock_only').fetchall()

    assert info.time_usable is False
    assert info.normalised_column is None
    assert all(col[0] != '_time_unix_s' for col in desc)
    conn.close()


def test_detect_and_normalise_time_ignores_large_memory_column() -> None:
    """Large byte metrics inside epoch range are not treated as timestamps."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        'CREATE TABLE metrics_only AS '
        'SELECT 1500000000 AS adservice_mem, 25.5 AS adservice_cpu'
    )
    cols = _columns_for(conn, 'metrics_only')

    info = time_detect.detect_and_normalise_time(conn, 'metrics_only', cols)

    assert info.time_usable is False
    assert info.normalised_column is None
    conn.close()


def test_detect_and_normalise_time_handles_empty_table() -> None:
    """Empty tables with timestamp schema flag time_usable=False cleanly."""
    conn = duckdb.connect(':memory:')
    conn.execute('CREATE TABLE empty_tbl (timestamp DOUBLE, val DOUBLE)')
    cols = _columns_for(conn, 'empty_tbl')

    info = time_detect.detect_and_normalise_time(conn, 'empty_tbl', cols)

    assert info.time_usable is False
    assert info.normalised_column is None
    conn.close()
