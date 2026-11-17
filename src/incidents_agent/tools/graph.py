"""Component dependency graph tool over detected relationship columns."""

from incidents_agent.ingest import loaders
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.ingest import time_detect
from incidents_agent.tools import safety
from incidents_agent.tools import schema as tool_schema
from incidents_agent.tools import tables


def _error_predicate_sql(status_col: str | None, alias_prefix: str) -> str:
    """Build a DuckDB boolean SQL expression indicating an error row."""
    if status_col is None:
        return 'FALSE'
    col_ref = f'{alias_prefix}{time_detect.quote_ident(status_col)}'
    lower_name = status_col.lower()
    if 'error' in lower_name:
        return (
            f'({col_ref} IS NOT NULL AND '
            f'LOWER(TRIM(CAST({col_ref} AS VARCHAR))) '
            f"NOT IN ('', '0', '0.0', 'false', 'ok', 'none', 'null'))"
        )
    # Handle gRPC statusCode (0 = OK), HTTP status (>= 400), or text status.
    return (
        f'({col_ref} IS NOT NULL AND ('
        f'(TRY_CAST({col_ref} AS DOUBLE) IS NOT NULL AND '
        f'(CASE WHEN TRY_CAST({col_ref} AS DOUBLE) >= 100 '
        f'THEN TRY_CAST({col_ref} AS DOUBLE) >= 400 '
        f'ELSE TRY_CAST({col_ref} AS DOUBLE) != 0 END)) '
        f'OR (TRY_CAST({col_ref} AS DOUBLE) IS NULL AND '
        f'LOWER(TRIM(CAST({col_ref} AS VARCHAR))) '
        f"IN ('error', 'err', 'fail', 'failed', 'timeout', 'unavailable'))"
        f'))'
    )


def _component_matches(edge_node: str, query_component: str) -> bool:
    """Match exact service names and common suffixes like frontendservice."""
    left = edge_node.strip().lower()
    right = query_component.strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    left_base = left.removesuffix('service')
    right_base = right.removesuffix('service')
    return left_base == right_base


def _query_edges(
    ctx: tables.ToolContext,
    entry: ingest_schema.TableCatalogEntry,
    rel: ingest_schema.RelationshipInfo,
    split_time: float | None,
) -> list[dict[str, object]]:
    """Aggregate directed edges with counts, error rates, and latencies."""
    table_sql = time_detect.quote_ident(entry.name)
    time_col = entry.time_info.normalised_column
    time_prefix = 'c.' if rel.kind == 'span_parent' else ''
    time_ref = (
        f'{time_prefix}{time_detect.quote_ident(time_col)}'
        if time_col is not None
        else 'NULL'
    )

    dur_col = rel.duration_column
    dur_prefix = 'c.' if rel.kind == 'span_parent' else ''
    dur_expr = (
        f'TRY_CAST({dur_prefix}{time_detect.quote_ident(dur_col)} AS DOUBLE)'
        if dur_col is not None
        else 'CAST(NULL AS DOUBLE)'
    )
    err_expr = _error_predicate_sql(
        rel.status_column, 'c.' if rel.kind == 'span_parent' else ''
    )

    if rel.kind == 'span_parent':
        assert rel.component_column is not None
        comp_sql = time_detect.quote_ident(rel.component_column)
        span_sql = time_detect.quote_ident(rel.source_column)
        parent_sql = time_detect.quote_ident(rel.target_column)
        from_clause = (
            f'FROM {table_sql} AS c '
            f'INNER JOIN {table_sql} AS p '
            f'ON TRIM(CAST(c.{parent_sql} AS VARCHAR)) = '
            f'TRIM(CAST(p.{span_sql} AS VARCHAR)) '
            f"WHERE NULLIF(TRIM(CAST(p.{comp_sql} AS VARCHAR)), '') "
            f'IS NOT NULL '
            f"AND NULLIF(TRIM(CAST(c.{comp_sql} AS VARCHAR)), '') "
            f'IS NOT NULL '
            f'AND TRIM(CAST(p.{comp_sql} AS VARCHAR)) != '
            f'TRIM(CAST(c.{comp_sql} AS VARCHAR))'
        )
        caller_expr = f'TRIM(CAST(p.{comp_sql} AS VARCHAR))'
        callee_expr = f'TRIM(CAST(c.{comp_sql} AS VARCHAR))'
    else:
        caller_sql = time_detect.quote_ident(rel.source_column)
        callee_sql = time_detect.quote_ident(rel.target_column)
        from_clause = (
            f'FROM {table_sql} '
            f"WHERE NULLIF(TRIM(CAST({caller_sql} AS VARCHAR)), '') "
            f'IS NOT NULL '
            f"AND NULLIF(TRIM(CAST({callee_sql} AS VARCHAR)), '') "
            f'IS NOT NULL'
        )
        caller_expr = f'TRIM(CAST({caller_sql} AS VARCHAR))'
        callee_expr = f'TRIM(CAST({callee_sql} AS VARCHAR))'

    if split_time is not None:
        before_cond = f'{time_ref} <= ?'
        after_cond = f'{time_ref} > ?'
        params: list[object] = [split_time] * 8
        metrics_sql = (
            f'COUNT(*) FILTER (WHERE {before_cond}) AS cnt_before, '
            f'COUNT(*) FILTER (WHERE {after_cond}) AS cnt_after, '
            f'AVG(CASE WHEN {err_expr} THEN 1.0 ELSE 0.0 END) '
            f'FILTER (WHERE {before_cond}) AS err_before, '
            f'AVG(CASE WHEN {err_expr} THEN 1.0 ELSE 0.0 END) '
            f'FILTER (WHERE {after_cond}) AS err_after, '
            f'AVG({dur_expr}) FILTER (WHERE {before_cond}) AS lat_mean_before, '
            f'AVG({dur_expr}) FILTER (WHERE {after_cond}) AS lat_mean_after, '
            f'QUANTILE_CONT({dur_expr}, 0.90) '
            f'FILTER (WHERE {before_cond}) AS lat_p90_before, '
            f'QUANTILE_CONT({dur_expr}, 0.90) '
            f'FILTER (WHERE {after_cond}) AS lat_p90_after '
        )
    else:
        params = []
        metrics_sql = (
            'CAST(NULL AS BIGINT) AS cnt_before, '
            'CAST(NULL AS BIGINT) AS cnt_after, '
            'CAST(NULL AS DOUBLE) AS err_before, '
            'CAST(NULL AS DOUBLE) AS err_after, '
            'CAST(NULL AS DOUBLE) AS lat_mean_before, '
            'CAST(NULL AS DOUBLE) AS lat_mean_after, '
            'CAST(NULL AS DOUBLE) AS lat_p90_before, '
            'CAST(NULL AS DOUBLE) AS lat_p90_after '
        )

    sql = (
        f'SELECT '
        f'{caller_expr} AS caller, '
        f'{callee_expr} AS callee, '
        f'COUNT(*) AS total_calls, '
        f'AVG(CASE WHEN {err_expr} THEN 1.0 ELSE 0.0 END) AS error_rate_total, '
        f'AVG({dur_expr}) AS mean_latency_total, '
        f'QUANTILE_CONT({dur_expr}, 0.90) AS p90_latency_total, '
        f'{metrics_sql}'
        f'{from_clause} '
        f'GROUP BY caller, callee '
        f'ORDER BY total_calls DESC, caller ASC, callee ASC'
    )

    rows = ctx.connection.execute(sql, params).fetchall()
    edges: list[dict[str, object]] = []
    for row in rows:
        edges.append(
            {
                'table': entry.name,
                'relationship_kind': rel.kind,
                'caller': str(row[0]),
                'callee': str(row[1]),
                'call_count_total': int(row[2] or 0),
                'error_rate_total': loaders.json_safe(row[3]),
                'mean_latency_total': loaders.json_safe(row[4]),
                'p90_latency_total': loaders.json_safe(row[5]),
                'call_count_before': (
                    int(row[6] or 0) if split_time is not None else None
                ),
                'call_count_after': (
                    int(row[7] or 0) if split_time is not None else None
                ),
                'error_rate_before': loaders.json_safe(row[8]),
                'error_rate_after': loaders.json_safe(row[9]),
                'mean_latency_before': loaders.json_safe(row[10]),
                'mean_latency_after': loaders.json_safe(row[11]),
                'p90_latency_before': loaders.json_safe(row[12]),
                'p90_latency_after': loaders.json_safe(row[13]),
            }
        )
    return edges


def dependency_graph(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Return component call edges and before/after error and latency stats."""
    raw_split = args.get('split_time')
    split_time: float | None = None
    if raw_split is not None:
        if not isinstance(raw_split, (int, float)) or isinstance(
            raw_split, bool
        ):
            return tables.error_result(
                'invalid_args', 'split_time must be a numeric timestamp'
            )
        split_time = float(raw_split)

    raw_component = args.get('component')
    if raw_component is not None and (
        not isinstance(raw_component, str) or not raw_component.strip()
    ):
        return tables.error_result(
            'invalid_args', 'component must be a non-empty string'
        )
    component = (
        raw_component.strip() if isinstance(raw_component, str) else None
    )

    raw_table = args.get('table')
    if raw_table is not None:
        if not isinstance(raw_table, str) or not raw_table.strip():
            return tables.error_result(
                'invalid_args', 'table must be a non-empty string'
            )
        explicit_entry = ctx.catalog.get_table(raw_table)
        if explicit_entry is None:
            return tables.error_result(
                'unknown_table', f'Table not found in catalog: {raw_table}'
            )
        candidate_tables = [explicit_entry]
    else:
        candidate_tables = list(ctx.catalog.tables)

    rel_tables = [t for t in candidate_tables if t.relationships]
    if not rel_tables:
        return {
            'ok': True,
            'has_relationships': False,
            'message': (
                'No relationship columns detected in catalog; call direction '
                'cannot be established from the files.'
            ),
            'untrusted_data': safety.wrap_untrusted_data({'edges': []}),
            'locator': tool_schema.ToolLocator(
                kind='dependency_graph',
                expression='no_relationships',
            ).to_dict(),
            'target_components': [],
        }

    all_edges: list[dict[str, object]] = []
    used_tables: list[str] = []
    for entry in rel_tables:
        if split_time is not None and not entry.time_info.time_usable:
            return tables.error_result(
                'unusable_time',
                f'Relationship table {entry.name!r} has no usable timestamp '
                f'mapping ({entry.time_info.reason}).',
            )
        used_tables.append(entry.name)
        for rel in entry.relationships:
            all_edges.extend(_query_edges(ctx, entry, rel, split_time))

    if component is not None:
        filtered = [
            edge
            for edge in all_edges
            if _component_matches(str(edge['caller']), component)
            or _component_matches(str(edge['callee']), component)
        ]
    else:
        filtered = all_edges

    # A quotable form of each edge, so evidence can cite it word for word.
    filtered = [
        {**edge, 'edge': f'{edge["caller"]} -> {edge["callee"]}'}
        for edge in filtered
    ]
    nodes: list[str] = []
    for edge in filtered:
        for role in ('caller', 'callee'):
            val = str(edge[role])
            if val not in nodes:
                nodes.append(val)

    return {
        'ok': True,
        'has_relationships': True,
        'summary': {
            'tables_used': used_tables,
            'edge_count': len(filtered),
            'component_filter': component,
            'split_time': split_time,
        },
        'untrusted_data': safety.wrap_untrusted_data({'edges': filtered}),
        'locator': tool_schema.ToolLocator(
            kind='dependency_graph',
            table=used_tables[0] if len(used_tables) == 1 else None,
            window=(None, split_time) if split_time is not None else None,
            expression=(
                f'component={component}' if component is not None else 'full'
            ),
        ).to_dict(),
        'target_components': nodes,
    }
