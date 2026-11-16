"""Regex text search and message-pattern grouping tool."""

import re

import duckdb

from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.ingest import time_detect
from incidents_agent.tools import safety
from incidents_agent.tools import schema as tool_schema
from incidents_agent.tools import tables

_PREFERRED_TEXT_COLS = (
    'message',
    'raw_text',
    'msg',
    'text',
    'error',
    'log',
)
_COMPONENT_COL_CANDIDATES = (
    'container_name',
    'servicename',
    'service_name',
    'service',
    'component',
    'container',
)

_TS_RE = re.compile(
    r'\b\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?\b'
)
_UUID_RE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
    re.IGNORECASE,
)
_IP_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b')
_HEX_RE = re.compile(r'\b[0-9a-f]{8,}\b', re.IGNORECASE)
_NUM_RE = re.compile(r'\b\d+(?:\.\d+)?\b')


def mask_message_pattern(text: str) -> str:
    """Normalise timestamps, UUIDs, IPs, and numbers into pattern tokens."""
    masked = _TS_RE.sub('<TS>', text.strip())
    masked = _UUID_RE.sub('<UUID>', masked)
    masked = _IP_RE.sub('<IP>', masked)
    masked = _HEX_RE.sub('<HEX>', masked)
    masked = _NUM_RE.sub('<NUM>', masked)
    return masked


def _resolve_text_column(
    entry: ingest_schema.TableCatalogEntry, explicit: object
) -> tuple[str | None, str | None]:
    """Return (column_name, error_message) for the target text column."""
    col_names = {col.name.lower(): col for col in entry.columns}
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            return (None, 'column must be a non-empty string')
        found = col_names.get(explicit.lower())
        if found is None:
            return (None, f'Column {explicit!r} not found in {entry.name}')
        return (found.name, None)

    for candidate in _PREFERRED_TEXT_COLS:
        found = col_names.get(candidate)
        if found is not None:
            return (found.name, None)

    for col in entry.columns:
        if 'CHAR' in col.dtype.upper() or 'TEXT' in col.dtype.upper():
            if col.name != entry.time_info.column:
                return (col.name, None)
    return (None, f'No text column found in table {entry.name}')


def search_text(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Search a text column by regex and group matches by message pattern.

    When `split_time` is provided, requires `time_usable=True` on the table
    and counts occurrences before (`<= split_time`) and after (`> split_time`).
    """
    table_name = args.get('table')
    if not isinstance(table_name, str) or not table_name.strip():
        return tables.error_result(
            'invalid_args', 'table must be a non-empty string'
        )

    entry = ctx.catalog.get_table(table_name)
    if entry is None:
        return tables.error_result(
            'unknown_table', f'Table not found in catalog: {table_name}'
        )

    raw_pattern = args.get('pattern')
    if not isinstance(raw_pattern, str) or not raw_pattern:
        return tables.error_result(
            'invalid_args', 'pattern must be a non-empty regex string'
        )
    try:
        compiled = re.compile(raw_pattern, re.IGNORECASE)
    except re.error as exc:
        return tables.error_result('invalid_regex', f'Invalid regex: {exc}')

    raw_top_k = args.get('top_k', 20)
    if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool):
        return tables.error_result('invalid_args', 'top_k must be an integer')
    top_k = max(1, min(raw_top_k, 100))

    col_name, col_err = _resolve_text_column(entry, args.get('column'))
    if col_err is not None or col_name is None:
        return tables.error_result('unknown_column', col_err or 'no text col')

    raw_split = args.get('split_time')
    split_time: float | None = None
    if raw_split is not None:
        if not isinstance(raw_split, (int, float)) or isinstance(
            raw_split, bool
        ):
            return tables.error_result(
                'invalid_args', 'split_time must be a numeric timestamp'
            )
        if (
            not entry.time_info.time_usable
            or entry.time_info.normalised_column is None
        ):
            return tables.error_result(
                'unusable_time',
                f'Table {entry.name!r} has no usable timestamp mapping; '
                f'cannot split counts ({entry.time_info.reason}).',
            )
        split_time = float(raw_split)

    table_sql = time_detect.quote_ident(entry.name)
    col_sql = time_detect.quote_ident(col_name)
    col_map = {col.name.lower(): col.name for col in entry.columns}
    line_no_col = col_map.get('line_no')
    comp_col: str | None = None
    for cand in _COMPONENT_COL_CANDIDATES:
        if cand in col_map:
            comp_col = col_map[cand]
            break

    time_col = entry.time_info.normalised_column
    time_expr = (
        time_detect.quote_ident(time_col) if time_col is not None else 'NULL'
    )
    line_expr = (
        time_detect.quote_ident(line_no_col)
        if line_no_col is not None
        else 'NULL'
    )
    comp_expr = (
        time_detect.quote_ident(comp_col) if comp_col is not None else 'NULL'
    )

    max_scan = ctx.catalog.limits.max_scan_rows
    select_cols = (
        f'CAST({col_sql} AS VARCHAR) AS txt, '
        f'{time_expr} AS ts, '
        f'{line_expr} AS lno, '
        f'{comp_expr} AS comp'
    )

    matched_rows: list[tuple[object, ...]] = []
    try:
        matched_rows = ctx.connection.execute(
            f'SELECT {select_cols} FROM {table_sql} '
            f'WHERE {col_sql} IS NOT NULL '
            f"AND regexp_matches(CAST({col_sql} AS VARCHAR), ?, 'i') "
            f'LIMIT {max_scan}',
            [raw_pattern],
        ).fetchall()
    except duckdb.Error:
        candidate_rows = ctx.connection.execute(
            f'SELECT {select_cols} FROM {table_sql} '
            f'WHERE {col_sql} IS NOT NULL LIMIT {max_scan}'
        ).fetchall()
        matched_rows = [
            row for row in candidate_rows if compiled.search(str(row[0]))
        ]

    groups: dict[str, dict[str, object]] = {}
    components_seen: set[str] = set()

    for raw_txt, ts_val, line_val, comp_val in matched_rows:
        text = str(raw_txt)
        template = mask_message_pattern(text)
        if comp_val is not None and str(comp_val).strip():
            components_seen.add(str(comp_val).strip())

        bucket = groups.get(template)
        if bucket is None:
            bucket = {
                'pattern_template': template,
                'total_count': 0,
                'count_before': 0 if split_time is not None else None,
                'count_after': 0 if split_time is not None else None,
                'sample_line_no': (
                    int(line_val) if isinstance(line_val, int) else None
                ),
                'sample_text': text,
            }
            groups[template] = bucket

        bucket['total_count'] = int(str(bucket['total_count'])) + 1
        if split_time is not None and isinstance(ts_val, (int, float)):
            if float(ts_val) <= split_time:
                bucket['count_before'] = int(str(bucket['count_before'])) + 1
            else:
                bucket['count_after'] = int(str(bucket['count_after'])) + 1

    ranked = sorted(
        groups.values(),
        key=lambda item: int(str(item['total_count'])),
        reverse=True,
    )[:top_k]

    window = (None, split_time) if split_time is not None else None
    return {
        'ok': True,
        'summary': {
            'table': entry.name,
            'column': col_name,
            'total_matches': len(matched_rows),
            'unique_patterns': len(groups),
            'returned_patterns': len(ranked),
            'split_time': split_time,
        },
        'untrusted_data': safety.wrap_untrusted_data({'matches': ranked}),
        'locator': tool_schema.ToolLocator(
            kind='text_search',
            table=entry.name,
            columns=(col_name,),
            window=window,
            expression=raw_pattern,
        ).to_dict(),
        'target_components': sorted(components_seen),
    }
