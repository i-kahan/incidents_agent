"""Detect caller/callee and span/parent-span relationship columns in tables."""

from collections.abc import Sequence

import duckdb

from incidents_agent.ingest import schema
from incidents_agent.ingest import time_detect

_CALLER_CALLEE_PAIRS: tuple[tuple[str, str], ...] = (
    ('caller', 'callee'),
    ('caller_service', 'callee_service'),
    ('callerservice', 'calleeservice'),
    ('source', 'destination'),
    ('source', 'dest'),
    ('src', 'dst'),
    ('source_service', 'destination_service'),
    ('sourceservice', 'destinationservice'),
    ('src_service', 'dst_service'),
    ('client', 'server'),
    ('client_service', 'server_service'),
    ('from_service', 'to_service'),
    ('upstream', 'downstream'),
)

_SPAN_ID_NAMES = frozenset({'spanid', 'span_id'})
_PARENT_SPAN_NAMES = frozenset(
    {'parentspanid', 'parent_span_id', 'parentid', 'parent_id'}
)
_COMPONENT_NAMES = (
    'servicename',
    'service_name',
    'service',
    'component',
    'container_name',
    'container',
    'app',
)
_DURATION_NAMES = (
    'duration',
    'duration_ms',
    'duration_us',
    'durationms',
    'durationus',
    'latency',
    'latency_ms',
)
_STATUS_NAMES = (
    'statuscode',
    'status_code',
    'status',
    'error',
    'is_error',
)


def _find_optional_column(
    lower_to_orig: dict[str, str],
    candidates: Sequence[str],
) -> str | None:
    """Return the first matching original column name from candidates."""
    for candidate in candidates:
        if candidate in lower_to_orig:
            return lower_to_orig[candidate]
    return None


def _compute_span_overlap(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    span_col: str,
    parent_col: str,
) -> tuple[int, float]:
    """Return (non_empty_parent_count, fraction matching span_col)."""
    table_sql = time_detect.quote_ident(table_name)
    span_sql = time_detect.quote_ident(span_col)
    parent_sql = time_detect.quote_ident(parent_col)
    row = conn.execute(
        f'SELECT '
        f'COUNT(*) FILTER (WHERE NULLIF(TRIM(CAST({parent_sql} AS VARCHAR)), '
        f"'') IS NOT NULL), "
        f'COUNT(*) FILTER (WHERE NULLIF(TRIM(CAST({parent_sql} AS VARCHAR)), '
        f"'') IS NOT NULL AND TRIM(CAST({parent_sql} AS VARCHAR)) IN ("
        f'SELECT DISTINCT TRIM(CAST({span_sql} AS VARCHAR)) FROM {table_sql} '
        f'WHERE NULLIF(TRIM(CAST({span_sql} AS VARCHAR)), '
        f"'') IS NOT NULL)) "
        f'FROM {table_sql}'
    ).fetchone()
    if row is None:
        return (0, 0.0)
    total_parents = int(row[0] or 0)
    matched = int(row[1] or 0)
    if total_parents == 0:
        return (0, 0.0)
    return (total_parents, matched / total_parents)


def _compute_caller_callee_overlap(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    caller_col: str,
    callee_col: str,
) -> tuple[int, float]:
    """Return (non_empty_pair_count, entity overlap ratio between columns)."""
    table_sql = time_detect.quote_ident(table_name)
    caller_sql = time_detect.quote_ident(caller_col)
    callee_sql = time_detect.quote_ident(callee_col)
    row = conn.execute(
        'SELECT COUNT(*) FILTER ('
        f"WHERE NULLIF(TRIM(CAST({caller_sql} AS VARCHAR)), '') IS NOT NULL "
        f"AND NULLIF(TRIM(CAST({callee_sql} AS VARCHAR)), '') IS NOT NULL"
        f') FROM {table_sql}'
    ).fetchone()
    pair_count = int(row[0] or 0) if row is not None else 0
    if pair_count == 0:
        return (0, 0.0)

    overlap_row = conn.execute(
        f'WITH callers AS ('
        f'SELECT DISTINCT TRIM(CAST({caller_sql} AS VARCHAR)) AS val '
        f'FROM {table_sql} '
        f"WHERE NULLIF(TRIM(CAST({caller_sql} AS VARCHAR)), '') IS NOT NULL"
        f'), callees AS ('
        f'SELECT DISTINCT TRIM(CAST({callee_sql} AS VARCHAR)) AS val '
        f'FROM {table_sql} '
        f"WHERE NULLIF(TRIM(CAST({callee_sql} AS VARCHAR)), '') IS NOT NULL"
        f') '
        f'SELECT '
        f'(SELECT COUNT(*) FROM callers INNER JOIN callees USING (val)), '
        f'(SELECT COUNT(*) FROM ('
        f'SELECT val FROM callers UNION SELECT val FROM callees))'
    ).fetchone()
    if overlap_row is None:
        return (pair_count, 0.0)
    intersection = int(overlap_row[0] or 0)
    union = int(overlap_row[1] or 0)
    ratio = (intersection / union) if union > 0 else 0.0
    return (pair_count, ratio)


def detect_relationships(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    columns: Sequence[schema.ColumnInfo],
) -> tuple[schema.RelationshipInfo, ...]:
    """Identify caller/callee or span/parent-span relationship columns.

    Args:
        conn: Readable or writable DuckDB connection containing `table_name`.
        table_name: Name of the table to inspect.
        columns: Column definitions for `table_name`.

    Returns:
        Tuple of RelationshipInfo records (empty when no relationship exists).
    """
    lower_to_orig = {col.name.lower(): col.name for col in columns}
    duration_col = _find_optional_column(lower_to_orig, _DURATION_NAMES)
    status_col = _find_optional_column(lower_to_orig, _STATUS_NAMES)
    relationships: list[schema.RelationshipInfo] = []

    span_col = _find_optional_column(lower_to_orig, sorted(_SPAN_ID_NAMES))
    parent_col = _find_optional_column(
        lower_to_orig, sorted(_PARENT_SPAN_NAMES)
    )
    component_col = _find_optional_column(lower_to_orig, _COMPONENT_NAMES)

    if (
        span_col is not None
        and parent_col is not None
        and component_col is not None
    ):
        non_empty_parents, overlap = _compute_span_overlap(
            conn, table_name, span_col, parent_col
        )
        if non_empty_parents > 0 and overlap > 0.0:
            confidence = round(min(1.0, 0.70 + 0.30 * overlap), 3)
            relationships.append(
                schema.RelationshipInfo(
                    kind='span_parent',
                    source_column=span_col,
                    target_column=parent_col,
                    component_column=component_col,
                    duration_column=duration_col,
                    status_column=status_col,
                    confidence=confidence,
                    overlap_ratio=round(overlap, 3),
                    reason=(
                        f'{span_col}/{parent_col} with {component_col} '
                        f'({overlap:.0%} parent overlap)'
                    ),
                )
            )

    for caller_hint, callee_hint in _CALLER_CALLEE_PAIRS:
        caller_col = lower_to_orig.get(caller_hint)
        callee_col = lower_to_orig.get(callee_hint)
        if caller_col is None or callee_col is None:
            continue
        pair_count, overlap = _compute_caller_callee_overlap(
            conn, table_name, caller_col, callee_col
        )
        if pair_count == 0:
            continue
        confidence = round(min(1.0, 0.80 + 0.20 * overlap), 3)
        relationships.append(
            schema.RelationshipInfo(
                kind='caller_callee',
                source_column=caller_col,
                target_column=callee_col,
                component_column=None,
                duration_column=duration_col,
                status_column=status_col,
                confidence=confidence,
                overlap_ratio=round(overlap, 3),
                reason=(
                    f'direct {caller_col}->{callee_col} edges '
                    f'({pair_count} rows, {overlap:.0%} entity overlap)'
                ),
            )
        )

    return tuple(relationships)
