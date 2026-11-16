"""Unit tests for relationship column detection in tables."""

import duckdb

from incidents_agent.ingest import relationships
from incidents_agent.ingest import schema


def _columns_for(
    conn: duckdb.DuckDBPyConnection, table_name: str
) -> tuple[schema.ColumnInfo, ...]:
    rows = conn.execute(f'DESCRIBE {table_name}').fetchall()
    return tuple(
        schema.ColumnInfo(name=str(row[0]), dtype=str(row[1])) for row in rows
    )


def test_detect_relationships_finds_caller_and_callee_columns() -> None:
    """Caller/callee tables detect direct edge columns with overlap ratio."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        'CREATE TABLE calls ('
        'caller VARCHAR, callee VARCHAR, duration_ms DOUBLE, status VARCHAR)'
    )
    conn.execute(
        'INSERT INTO calls VALUES '
        "('api', 'orders', 12.0, 'ok'), "
        "('orders', 'db', 45.0, 'error')"
    )
    cols = _columns_for(conn, 'calls')

    rels = relationships.detect_relationships(conn, 'calls', cols)

    assert len(rels) == 1
    assert rels[0].kind == 'caller_callee'
    assert rels[0].source_column == 'caller'
    assert rels[0].target_column == 'callee'
    assert rels[0].duration_column == 'duration_ms'
    assert rels[0].status_column == 'status'
    assert rels[0].overlap_ratio > 0.0
    conn.close()


def test_detect_relationships_finds_span_and_parent_span_ids() -> None:
    """Trace tables with matching spanID/parentSpanID register span_parent."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        'CREATE TABLE traces ('
        'spanID VARCHAR, parentSpanID VARCHAR, serviceName VARCHAR, '
        'duration BIGINT, statusCode DOUBLE)'
    )
    conn.execute(
        'INSERT INTO traces VALUES '
        "('s1', '', 'frontend', 100, 0.0), "
        "('s2', 's1', 'checkout', 50, 0.0), "
        "('s3', 's2', 'payment', 20, 4.0)"
    )
    cols = _columns_for(conn, 'traces')

    rels = relationships.detect_relationships(conn, 'traces', cols)

    assert len(rels) == 1
    assert rels[0].kind == 'span_parent'
    assert rels[0].source_column == 'spanID'
    assert rels[0].target_column == 'parentSpanID'
    assert rels[0].component_column == 'serviceName'
    assert rels[0].duration_column == 'duration'
    assert rels[0].status_column == 'statusCode'
    assert rels[0].overlap_ratio == 1.0
    conn.close()


def test_detect_relationships_rejects_disjoint_span_ids() -> None:
    """Span columns with zero overlap between parentSpanID and spanID ignore."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        'CREATE TABLE broken_traces ('
        'spanID VARCHAR, parentSpanID VARCHAR, serviceName VARCHAR)'
    )
    conn.execute(
        "INSERT INTO broken_traces VALUES ('s1', 'missing-parent', 'frontend')"
    )
    cols = _columns_for(conn, 'broken_traces')

    rels = relationships.detect_relationships(conn, 'broken_traces', cols)

    assert rels == ()
    conn.close()


def test_detect_relationships_returns_empty_for_metrics_table() -> None:
    """Plain metric tables have no relationships recorded."""
    conn = duckdb.connect(':memory:')
    conn.execute(
        'CREATE TABLE metrics '
        '(time BIGINT, frontend_cpu DOUBLE, redis_mem BIGINT)'
    )
    conn.execute('INSERT INTO metrics VALUES (1700000000, 12.5, 4096)')
    cols = _columns_for(conn, 'metrics')

    rels = relationships.detect_relationships(conn, 'metrics', cols)

    assert rels == ()
    conn.close()
