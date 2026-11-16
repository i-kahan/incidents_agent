"""Catalog inspection and read-only SQL execution tools."""

import dataclasses
import pathlib
import threading

import duckdb

from incidents_agent.ingest import loaders
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.ingest import time_detect
from incidents_agent.tools import safety
from incidents_agent.tools import schema as tool_schema

_FORBIDDEN_SQL_KEYWORDS = frozenset(
    {
        'ALTER',
        'ANALYZE',
        'ATTACH',
        'BEGIN',
        'CALL',
        'CHECKPOINT',
        'COMMIT',
        'COPY',
        'CREATE',
        'DELETE',
        'DESCRIBE',
        'DETACH',
        'DROP',
        'EXECUTE',
        'EXPORT',
        'GLOB',
        'GRANT',
        'IMPORT',
        'INSERT',
        'INSTALL',
        'LOAD',
        'MERGE',
        'PARQUET_SCAN',
        'PRAGMA',
        'PREPARE',
        'READ_BLOB',
        'READ_CSV',
        'READ_CSV_AUTO',
        'READ_JSON',
        'READ_JSON_AUTO',
        'READ_NDJSON',
        'READ_NDJSON_AUTO',
        'READ_PARQUET',
        'READ_TEXT',
        'REPLACE',
        'RESET',
        'REVOKE',
        'ROLLBACK',
        'SET',
        'SHOW',
        'SUMMARIZE',
        'TRUNCATE',
        'UPDATE',
        'UPSERT',
        'USE',
        'VACUUM',
    }
)


@dataclasses.dataclass(frozen=True)
class ToolContext:
    """Shared read-only database and catalog context passed to all tools.

    Attributes:
        connection: Read-only DuckDB connection with external access off.
        catalog: Ingest catalog built for the incident folder.
        db_path: Path to the underlying DuckDB file on disk.
    """

    connection: duckdb.DuckDBPyConnection
    catalog: ingest_schema.Catalog
    db_path: pathlib.Path | None = None


def error_result(
    code: str, message: str, extra: dict[str, object] | None = None
) -> dict[str, object]:
    """Return a structured tool error dictionary."""
    err: dict[str, object] = {'code': code, 'message': message}
    if extra:
        err.update(extra)
    return {'ok': False, 'error': err}


def _scan_sql_tokens(query: str) -> tuple[str, list[str], int]:
    """Strip comments outside quotes and extract bare tokens and ; count."""
    cleaned: list[str] = []
    tokens: list[str] = []
    semicolons = 0
    idx = 0
    length = len(query)
    in_single = False
    in_double = False

    while idx < length:
        ch = query[idx]
        nxt = query[idx + 1] if idx + 1 < length else ''

        if not in_single and not in_double:
            if ch == '-' and nxt == '-':
                end_line = query.find('\n', idx + 2)
                idx = length if end_line == -1 else end_line
                cleaned.append(' ')
                continue
            if ch == '/' and nxt == '*':
                end_block = query.find('*/', idx + 2)
                if end_block == -1:
                    raise ValueError('unterminated SQL block comment')
                idx = end_block + 2
                cleaned.append(' ')
                continue

        if ch == "'" and not in_double:
            cleaned.append(ch)
            if in_single and nxt == "'":
                cleaned.append("'")
                idx += 2
                continue
            in_single = not in_single
            idx += 1
            continue

        if ch == '"' and not in_single:
            cleaned.append(ch)
            if in_double and nxt == '"':
                cleaned.append('"')
                idx += 2
                continue
            in_double = not in_double
            idx += 1
            continue

        if not in_single and not in_double:
            if ch == ';':
                semicolons += 1
                cleaned.append(ch)
                idx += 1
                continue
            if ch.isalpha() or ch == '_':
                start = idx
                while idx < length and (
                    query[idx].isalnum() or query[idx] == '_'
                ):
                    idx += 1
                word = query[start:idx]
                tokens.append(word.upper())
                cleaned.append(word)
                continue

        cleaned.append(ch)
        idx += 1

    if in_single or in_double:
        raise ValueError('unterminated SQL string or identifier quote')
    return (''.join(cleaned).strip(), tokens, semicolons)


def validate_readonly_sql(query: str) -> str:
    """Validate that `query` is a single read-only SELECT or WITH statement.

    Args:
        query: Candidate SQL string supplied by the agent or user.

    Returns:
        Normalised single-statement SQL without trailing semicolon.

    Raises:
        ValueError: When the query is empty, multi-statement, non-SELECT, or
            references forbidden DDL/DML/external-access keywords.
    """
    cleaned, tokens, semicolons = _scan_sql_tokens(query)
    if not cleaned or not tokens:
        raise ValueError('SQL query must not be empty')

    if semicolons > 1:
        raise ValueError('multi-statement SQL queries are forbidden')
    if semicolons == 1:
        if not cleaned.endswith(';'):
            raise ValueError('multi-statement SQL queries are forbidden')
        cleaned = cleaned[:-1].strip()
        if not cleaned:
            raise ValueError('SQL query must not be empty')

    if tokens[0] not in ('SELECT', 'WITH'):
        raise ValueError(f'only SELECT queries are permitted, got {tokens[0]}')
    if tokens[0] == 'WITH' and 'SELECT' not in tokens[1:]:
        raise ValueError('WITH query must contain a SELECT statement')

    forbidden = [tok for tok in tokens if tok in _FORBIDDEN_SQL_KEYWORDS]
    if forbidden:
        raise ValueError(f'forbidden SQL keyword or function: {forbidden[0]}')
    return cleaned


def _extract_referenced_tables(
    tokens: list[str], catalog: ingest_schema.Catalog
) -> tuple[str, ...]:
    """Identify known table names referenced in SQL bare tokens."""
    known = {entry.name.upper(): entry.name for entry in catalog.tables}
    known['DOCUMENT_CHUNKS'] = 'document_chunks'
    found: list[str] = []
    for tok in tokens:
        matched = known.get(tok)
        if matched is not None and matched not in found:
            found.append(matched)
    return tuple(found)


def list_tables(ctx: ToolContext, args: dict[str, object]) -> dict[str, object]:
    """Return the ingested catalog overview of tables, documents, and skips."""
    _ = args
    tables_summary = [
        {
            'name': table.name,
            'source_file': table.source_file,
            'kind': table.kind,
            'row_count': table.row_count,
            'column_count': len(table.columns),
            'time_usable': table.time_info.time_usable,
            'time_column': table.time_info.column,
            'time_range_s': (
                [table.time_info.min_time_s, table.time_info.max_time_s]
                if table.time_info.time_usable
                else None
            ),
            'has_relationships': bool(table.relationships),
        }
        for table in ctx.catalog.tables
    ]
    documents_untrusted = [
        {
            'doc_id': doc.doc_id,
            'source_file': doc.source_file,
            'title': doc.title,
            'doc_type': doc.doc_type,
            'chunk_strategy': doc.chunk_strategy,
            'size_bytes': doc.size_bytes,
            'chunk_count': doc.chunk_count,
            'outline': [item.to_dict() for item in doc.outline],
        }
        for doc in ctx.catalog.documents
    ]
    return {
        'ok': True,
        'summary': {
            'table_count': len(tables_summary),
            'document_count': len(documents_untrusted),
            'skipped_count': len(ctx.catalog.skipped_files),
            'limits_hit': list(ctx.catalog.limits_hit),
        },
        'tables': tables_summary,
        'skipped_files': [skip.to_dict() for skip in ctx.catalog.skipped_files],
        'untrusted_data': safety.wrap_untrusted_data(
            {'documents': documents_untrusted}
        ),
        'locator': tool_schema.ToolLocator(kind='catalog').to_dict(),
        'target_components': [],
    }


def describe_table(
    ctx: ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Describe columns, summary statistics, time info, and sample rows."""
    table_name = args.get('table')
    if not isinstance(table_name, str) or not table_name.strip():
        return error_result('invalid_args', 'table must be a non-empty string')

    entry = ctx.catalog.get_table(table_name)
    if entry is None:
        return error_result(
            'unknown_table', f'Table not found in catalog: {table_name}'
        )

    raw_samples = args.get('sample_rows', 3)
    if not isinstance(raw_samples, int) or isinstance(raw_samples, bool):
        return error_result('invalid_args', 'sample_rows must be an integer')
    sample_count = max(0, min(10, raw_samples))

    table_sql = time_detect.quote_ident(entry.name)
    col_stats: list[dict[str, object]] = []
    for col in entry.columns:
        col_sql = time_detect.quote_ident(col.name)
        upper_dtype = col.dtype.upper()
        is_numeric = any(
            tok in upper_dtype
            for tok in ('INT', 'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMERIC')
        )
        if entry.row_count == 0:
            col_stats.append(
                {
                    'name': col.name,
                    'dtype': col.dtype,
                    'null_count': 0,
                    'is_numeric': is_numeric,
                }
            )
            continue

        if is_numeric:
            stat_row = ctx.connection.execute(
                f'SELECT '
                f'COUNT(*) FILTER (WHERE {col_sql} IS NULL), '
                f'MIN(CAST({col_sql} AS DOUBLE)), '
                f'MEDIAN(CAST({col_sql} AS DOUBLE)), '
                f'MAX(CAST({col_sql} AS DOUBLE)), '
                f'AVG(CAST({col_sql} AS DOUBLE)), '
                f'STDDEV_SAMP(CAST({col_sql} AS DOUBLE)) '
                f'FROM {table_sql}'
            ).fetchone()
            col_stats.append(
                {
                    'name': col.name,
                    'dtype': col.dtype,
                    'null_count': int(stat_row[0]) if stat_row else 0,
                    'is_numeric': True,
                    'min': loaders.json_safe(stat_row[1]) if stat_row else None,
                    'median': (
                        loaders.json_safe(stat_row[2]) if stat_row else None
                    ),
                    'max': loaders.json_safe(stat_row[3]) if stat_row else None,
                    'mean': (
                        loaders.json_safe(stat_row[4]) if stat_row else None
                    ),
                    'std': loaders.json_safe(stat_row[5]) if stat_row else None,
                }
            )
        else:
            null_row = ctx.connection.execute(
                f'SELECT COUNT(*) FILTER (WHERE {col_sql} IS NULL) '
                f'FROM {table_sql}'
            ).fetchone()
            col_stats.append(
                {
                    'name': col.name,
                    'dtype': col.dtype,
                    'null_count': int(null_row[0]) if null_row else 0,
                    'is_numeric': False,
                }
            )

    samples: list[dict[str, object]] = []
    if sample_count > 0:
        col_names = [col.name for col in entry.columns]
        rows = ctx.connection.execute(
            f'SELECT * FROM {table_sql} LIMIT {sample_count}'
        ).fetchall()
        for row in rows:
            samples.append(
                {
                    name: loaders.json_safe(val)
                    for name, val in zip(col_names, row, strict=True)
                }
            )

    return {
        'ok': True,
        'table': entry.name,
        'source_file': entry.source_file,
        'kind': entry.kind,
        'row_count': entry.row_count,
        'time_info': entry.time_info.to_dict(),
        'relationships': [rel.to_dict() for rel in entry.relationships],
        'columns': col_stats,
        'untrusted_data': safety.wrap_untrusted_data({'sample_rows': samples}),
        'locator': tool_schema.ToolLocator(
            kind='table_schema',
            table=entry.name,
            columns=tuple(col.name for col in entry.columns),
        ).to_dict(),
        'target_components': [],
    }


def run_sql(ctx: ToolContext, args: dict[str, object]) -> dict[str, object]:
    """Execute a read-only SELECT query with row limits and timeout."""
    raw_query = args.get('query')
    if not isinstance(raw_query, str) or not raw_query.strip():
        return error_result('invalid_args', 'query must be a non-empty string')

    raw_limit = args.get('limit', 100)
    if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
        return error_result('invalid_args', 'limit must be an integer')
    max_rows = ctx.catalog.limits.max_query_rows
    effective_limit = max(1, min(raw_limit, max_rows))
    limit_clamped = raw_limit > max_rows

    try:
        validated_sql = validate_readonly_sql(raw_query)
        _, tokens, _ = _scan_sql_tokens(validated_sql)
    except ValueError as exc:
        return error_result('invalid_sql', str(exc))

    wrapped_sql = (
        f'SELECT * FROM ({validated_sql}) AS _sub LIMIT {effective_limit + 1}'
    )
    timeout_s = max(0.05, ctx.catalog.limits.query_timeout_s)
    timed_out = False

    def _on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        ctx.connection.interrupt()

    timer = threading.Timer(timeout_s, _on_timeout)
    timer.start()
    try:
        cursor = ctx.connection.execute(wrapped_sql)
        col_names = [str(desc[0]) for desc in (cursor.description or [])]
        fetched = cursor.fetchall()
    except duckdb.InterruptException:
        return error_result(
            'query_timeout',
            f'SQL query exceeded timeout of {timeout_s:.2f}s',
        )
    except duckdb.Error as exc:
        if timed_out:
            return error_result(
                'query_timeout',
                f'SQL query exceeded timeout of {timeout_s:.2f}s',
            )
        return error_result('sql_error', str(exc))
    finally:
        timer.cancel()

    has_more = len(fetched) > effective_limit
    limited_rows = fetched[:effective_limit]
    dict_rows = [
        {
            col_name: loaders.json_safe(val)
            for col_name, val in zip(col_names, row, strict=True)
        }
        for row in limited_rows
    ]
    tables_used = _extract_referenced_tables(tokens, ctx.catalog)

    return {
        'ok': True,
        'summary': {
            'columns': col_names,
            'row_count': len(dict_rows),
            'has_more': has_more,
            'limit_applied': effective_limit,
            'limit_clamped_to_max_query_rows': limit_clamped,
            'tables_referenced': list(tables_used),
        },
        'untrusted_data': safety.wrap_untrusted_data({'rows': dict_rows}),
        'locator': tool_schema.ToolLocator(
            kind='sql_query',
            table=tables_used[0] if len(tables_used) == 1 else None,
            columns=tuple(col_names),
            expression=validated_sql,
        ).to_dict(),
        'target_components': [],
    }
