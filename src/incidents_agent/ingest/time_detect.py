"""Time-column detection and Unix-seconds normalisation for ingested tables."""

import datetime
import math
import re
from collections.abc import Sequence

import duckdb

from incidents_agent.ingest import schema

_MIN_EPOCH_S = 946_684_800.0  # 2000-01-01T00:00:00Z
_MAX_EPOCH_S = 4_102_444_800.0  # 2100-01-01T00:00:00Z

_EXACT_STRONG_NAMES = frozenset(
    {
        'timestamp',
        'starttimemillis',
        'starttime',
        'time_unix',
        'unix_time',
        'event_time',
        'created_at',
        'occurred_at',
        'datetime',
        'ts',
    }
)
_EXACT_MEDIUM_NAMES = frozenset({'time', 'date', 'at'})
_EXCLUDED_SUBSTRINGS = (
    'duration',
    'latency',
    'timeout',
    'uptime',
    'elapsed',
    'count',
    'size',
    'cpu',
    'mem',
    'disk',
    'socket',
    'workload',
    'error',
    'port',
    'pid',
    'status',
    'line_no',
    'spanid',
    'traceid',
)
_CLOCK_ONLY_RE = re.compile(r'^\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?$')
_NUMERIC_STR_RE = re.compile(r'^[+-]?\d+(?:\.\d+)?$')
_OFFSET_RE = re.compile(r'(?:Z|[+-]\d{2}:?\d{2})$', re.IGNORECASE)


def quote_ident(identifier: str) -> str:
    """Safely double-quote a SQL identifier for DuckDB queries."""
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _name_hint_score(name: str) -> float:
    """Return a [0.0, 1.0] hint score based on a column's name."""
    lower = name.lower()
    if lower in _EXACT_STRONG_NAMES:
        return 0.95
    if lower in _EXACT_MEDIUM_NAMES:
        return 0.85
    if any(token in lower for token in _EXCLUDED_SUBSTRINGS):
        return 0.0
    if 'timestamp' in lower or 'time' in lower or lower.endswith('_ts'):
        return 0.65
    return 0.0


def _infer_numeric_unit(
    values: Sequence[float],
) -> tuple[schema.TimeUnit, float, str] | None:
    """Infer epoch unit when >= 90% of finite values fit a modern range."""
    finite = [val for val in values if math.isfinite(val)]
    if not finite:
        return None
    scales: tuple[tuple[schema.TimeUnit, float], ...] = (
        ('unix_s', 1.0),
        ('unix_ms', 1_000.0),
        ('unix_us', 1_000_000.0),
        ('unix_ns', 1_000_000_000.0),
    )
    for unit, divisor in scales:
        in_range = sum(
            1
            for val in finite
            if _MIN_EPOCH_S <= (val / divisor) <= _MAX_EPOCH_S
        )
        ratio = in_range / len(finite)
        if ratio >= 0.90:
            return (
                unit,
                ratio,
                f'{ratio:.0%} of sampled numeric values match {unit}',
            )
    return None


def _parse_iso_timestamp(text: str) -> tuple[bool, bool]:
    """Return (parsed_ok, has_explicit_offset) for a candidate datetime."""
    cleaned = text.strip().replace('/', '-')
    cleaned = re.sub(r',(\d{1,9})', r'.\1', cleaned)
    if _CLOCK_ONLY_RE.match(cleaned):
        return (False, False)
    if len(cleaned) < 8:
        return (False, False)
    has_offset = bool(_OFFSET_RE.search(cleaned))
    if cleaned.endswith(('Z', 'z')):
        cleaned = f'{cleaned[:-1]}+00:00'
    try:
        datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return (False, False)
    return (True, has_offset)


def _score_column(
    name: str,
    dtype: str,
    samples: Sequence[object],
) -> tuple[schema.TimeUnit, float, str, str]:
    """Evaluate how strongly a column's values represent an absolute time."""
    if not samples:
        return ('unknown', 0.0, 'UTC', 'no non-null values in column')

    upper_dtype = dtype.upper()
    if 'TIMESTAMP' in upper_dtype or 'DATETIME' in upper_dtype:
        tz_label = (
            'UTC (explicit TZ type)'
            if 'WITH TIME ZONE' in upper_dtype or 'TZ' in upper_dtype
            else 'UTC (assumed for naive TIMESTAMP)'
        )
        return ('iso8601', 0.98, tz_label, f'DuckDB {dtype} column')

    if any(
        tok in upper_dtype
        for tok in ('INT', 'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMERIC')
    ):
        floats: list[float] = []
        for raw in samples:
            if isinstance(raw, (int, float)):
                floats.append(float(raw))
        inferred = _infer_numeric_unit(floats)
        if inferred is None:
            return (
                'unknown',
                0.0,
                'UTC',
                'numeric values outside epoch range (2000-2100)',
            )
        unit, ratio, reason = inferred
        return (unit, ratio, 'UTC (Unix epoch)', reason)

    texts = [str(raw).strip() for raw in samples if str(raw).strip()]
    if not texts:
        return ('unknown', 0.0, 'UTC', 'only empty strings in column')

    clock_count = sum(1 for item in texts if _CLOCK_ONLY_RE.match(item))
    if clock_count / len(texts) >= 0.80:
        return (
            'unknown',
            0.30,
            'unknown',
            'clock-only time string without date (cannot map reliably)',
        )

    numeric_vals: list[float] = []
    for item in texts:
        if _NUMERIC_STR_RE.match(item):
            numeric_vals.append(float(item))
    if len(numeric_vals) / len(texts) >= 0.90:
        inferred = _infer_numeric_unit(numeric_vals)
        if inferred is not None:
            unit, ratio, reason = inferred
            return (unit, 0.95 * ratio, 'UTC (Unix epoch)', reason)

    iso_ok = 0
    offset_count = 0
    for item in texts:
        parsed, has_offset = _parse_iso_timestamp(item)
        if parsed:
            iso_ok += 1
            if has_offset:
                offset_count += 1
    iso_ratio = iso_ok / len(texts)
    if iso_ratio >= 0.85:
        tz_note = (
            'explicit offset normalised to UTC'
            if offset_count > 0
            else 'UTC (assumed for naive timestamps)'
        )
        return (
            'iso8601',
            0.95 * iso_ratio,
            tz_note,
            f'{iso_ratio:.0%} of sampled strings parse as ISO-8601 timestamps',
        )

    return ('unknown', 0.0, 'UTC', 'values do not match supported time formats')


def _normalisation_sql(column: str, unit: schema.TimeUnit) -> str:
    """Build the DuckDB SQL expression returning Unix seconds as DOUBLE."""
    col_sql = quote_ident(column)
    if unit == 'unix_s':
        return f'TRY_CAST({col_sql} AS DOUBLE)'
    if unit == 'unix_ms':
        return f'(TRY_CAST({col_sql} AS DOUBLE) / 1000.0)'
    if unit == 'unix_us':
        return f'(TRY_CAST({col_sql} AS DOUBLE) / 1000000.0)'
    if unit == 'unix_ns':
        return f'(TRY_CAST({col_sql} AS DOUBLE) / 1000000000.0)'
    cleaned = (
        f"regexp_replace(replace(CAST({col_sql} AS VARCHAR), '/', '-'), "
        r"',(\d{1,9})', '.\1')"
    )
    return (
        'CASE '
        f"WHEN regexp_matches({cleaned}, '(?i)(Z|[+-]\\d{{2}}:?\\d{{2}})$') "
        f'THEN epoch(TRY_CAST({cleaned} AS TIMESTAMPTZ)) '
        f'ELSE epoch(TRY_CAST({cleaned} AS TIMESTAMP)) '
        'END'
    )


def detect_and_normalise_time(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    columns: Sequence[schema.ColumnInfo],
    confidence_threshold: float = 0.70,
) -> schema.TimeColumnInfo:
    """Detect the primary timestamp column and add `_time_unix_s` when usable.

    The original column is kept untouched. Tables whose best candidate falls
    below `confidence_threshold` or has no valid timestamp values are flagged
    with `time_usable=False` and no `_time_unix_s` column is guessed.

    Args:
        conn: Writable DuckDB connection containing `table_name`.
        table_name: Name of the table to inspect.
        columns: Columns belonging to `table_name`.
        confidence_threshold: Minimum confidence required to mark time usable.

    Returns:
        TimeColumnInfo describing the selected column, inferred unit,
        timezone assumption, confidence, range, and usability flag.
    """
    table_sql = quote_ident(table_name)
    best_score = -1.0
    best_col: str | None = None
    best_unit: schema.TimeUnit = 'unknown'
    best_tz = 'unknown'
    best_reason = 'no time column detected'

    for col in columns:
        name_score = _name_hint_score(col.name)
        is_ts_dtype = 'TIMESTAMP' in col.dtype.upper()
        if name_score == 0.0 and not is_ts_dtype and 'VARCHAR' not in col.dtype:
            continue

        col_sql = quote_ident(col.name)
        upper_dtype = col.dtype.upper()
        select_expr = (
            f'CAST({col_sql} AS VARCHAR)'
            if 'WITH TIME ZONE' in upper_dtype or 'TIMESTAMPTZ' in upper_dtype
            else col_sql
        )
        rows = conn.execute(
            f'SELECT {select_expr} FROM {table_sql} '
            f'WHERE {col_sql} IS NOT NULL LIMIT 200'
        ).fetchall()
        samples = [row[0] for row in rows]
        unit, value_score, tz_note, reason = _score_column(
            col.name, col.dtype, samples
        )

        if unit == 'unknown' or value_score < 0.80:
            combined = min(0.45, 0.30 * name_score + 0.50 * value_score)
        elif name_score > 0.0:
            combined = round(
                min(1.0, 0.30 * name_score + 0.70 * value_score), 3
            )
        else:
            combined = round(0.75 * value_score, 3)

        if combined > best_score:
            best_score = combined
            best_col = col.name
            best_unit = unit
            best_tz = tz_note
            best_reason = reason

    if best_col is None or best_score < confidence_threshold:
        return schema.TimeColumnInfo(
            column=best_col,
            normalised_column=None,
            unit=best_unit,
            timezone_assumption=best_tz,
            confidence=max(0.0, round(best_score, 3)),
            min_time_s=None,
            max_time_s=None,
            time_usable=False,
            reason=best_reason,
        )

    expr = _normalisation_sql(best_col, best_unit)
    norm_col = '_time_unix_s'
    conn.execute(f'ALTER TABLE {table_sql} ADD COLUMN {norm_col} DOUBLE')
    conn.execute(f'UPDATE {table_sql} SET {norm_col} = {expr}')

    stats = conn.execute(
        f'SELECT MIN({norm_col}), MAX({norm_col}), COUNT({norm_col}) '
        f'FROM {table_sql}'
    ).fetchone()
    min_t = float(stats[0]) if stats and stats[0] is not None else None
    max_t = float(stats[1]) if stats and stats[1] is not None else None
    non_null_count = int(stats[2]) if stats and stats[2] is not None else 0

    if min_t is None or max_t is None or non_null_count == 0:
        conn.execute(f'ALTER TABLE {table_sql} DROP COLUMN {norm_col}')
        return schema.TimeColumnInfo(
            column=best_col,
            normalised_column=None,
            unit=best_unit,
            timezone_assumption=best_tz,
            confidence=0.20,
            min_time_s=None,
            max_time_s=None,
            time_usable=False,
            reason='normalised timestamp column produced only NULL values',
        )

    return schema.TimeColumnInfo(
        column=best_col,
        normalised_column=norm_col,
        unit=best_unit,
        timezone_assumption=best_tz,
        confidence=round(best_score, 3),
        min_time_s=min_t,
        max_time_s=max_t,
        time_usable=True,
        reason=best_reason,
    )
