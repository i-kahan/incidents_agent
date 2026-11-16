"""Numeric window comparison, time-series downsampling, and onset detection."""

import math
import statistics
from collections.abc import Sequence

from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.ingest import time_detect
from incidents_agent.tools import schema as tool_schema
from incidents_agent.tools import tables

_IGNORED_NUMERIC_COLS = frozenset(
    {
        '_time_unix_s',
        'line_no',
        'starttimemillis',
        'starttime',
        'timestamp',
        'time',
    }
)
_VALID_AGGS = frozenset({'avg', 'min', 'max', 'sum', 'count'})


def _is_numeric_col(col: ingest_schema.ColumnInfo) -> bool:
    upper = col.dtype.upper()
    return any(
        tok in upper for tok in ('INT', 'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMERIC')
    )


def _require_usable_time_table(
    ctx: tables.ToolContext,
    raw_table: object,
) -> tuple[
    ingest_schema.TableCatalogEntry | None,
    str | None,
    dict[str, object] | None,
]:
    """Validate table existence, non-emptiness, and usable timestamp mapping."""
    if not isinstance(raw_table, str) or not raw_table.strip():
        return (
            None,
            None,
            tables.error_result(
                'invalid_args', 'table must be a non-empty string'
            ),
        )
    entry = ctx.catalog.get_table(raw_table)
    if entry is None:
        return (
            None,
            None,
            tables.error_result(
                'unknown_table', f'Table not found in catalog: {raw_table}'
            ),
        )
    if (
        not entry.time_info.time_usable
        or entry.time_info.normalised_column is None
    ):
        return (
            None,
            None,
            tables.error_result(
                'unusable_time',
                f'Table {entry.name!r} has no usable timestamp mapping '
                f'({entry.time_info.reason}).',
            ),
        )
    if entry.row_count == 0:
        return (
            None,
            None,
            tables.error_result(
                'empty_table', f'Table {entry.name!r} has zero rows.'
            ),
        )
    return (entry, entry.time_info.normalised_column, None)


def _candidate_numeric_columns(
    entry: ingest_schema.TableCatalogEntry,
    requested: object,
) -> tuple[list[str] | None, dict[str, object] | None]:
    """Resolve numeric columns for analysis or return a structured error."""
    by_name = {col.name: col for col in entry.columns}
    if requested is not None:
        if not isinstance(requested, list) or not all(
            isinstance(item, str) for item in requested
        ):
            return (
                None,
                tables.error_result(
                    'invalid_args', 'columns must be a list of strings'
                ),
            )
        selected: list[str] = []
        for name in requested:
            col = by_name.get(name)
            if col is None:
                return (
                    None,
                    tables.error_result(
                        'unknown_column',
                        f'Column {name!r} not found in {entry.name}',
                    ),
                )
            if not _is_numeric_col(col):
                return (
                    None,
                    tables.error_result(
                        'non_numeric_column',
                        f'Column {name!r} is not numeric ({col.dtype})',
                    ),
                )
            selected.append(col.name)
        return (selected, None)

    auto_cols = [
        col.name
        for col in entry.columns
        if _is_numeric_col(col)
        and col.name != entry.time_info.column
        and col.name.lower() not in _IGNORED_NUMERIC_COLS
    ]
    if not auto_cols:
        return (
            None,
            tables.error_result(
                'no_numeric_columns',
                f'Table {entry.name!r} has no numeric metric columns',
            ),
        )
    return (auto_cols, None)


def _fetch_window_stats(
    ctx: tables.ToolContext,
    table_name: str,
    time_col: str,
    columns: Sequence[str],
    split_time: float,
) -> dict[str, dict[str, dict[str, float | int | None]]]:
    """Compute before and after summary statistics per column in batches."""
    table_sql = time_detect.quote_ident(table_name)
    time_sql = time_detect.quote_ident(time_col)
    stats: dict[str, dict[str, dict[str, float | int | None]]] = {
        col: {} for col in columns
    }

    batch_size = 15
    for start in range(0, len(columns), batch_size):
        batch = columns[start : start + batch_size]
        exprs: list[str] = []
        for idx, col in enumerate(batch):
            col_sql = time_detect.quote_ident(col)
            exprs.append(f'COUNT({col_sql}) AS c_{idx}')
            exprs.append(f'AVG(CAST({col_sql} AS DOUBLE)) AS m_{idx}')
            exprs.append(f'STDDEV_SAMP(CAST({col_sql} AS DOUBLE)) AS s_{idx}')
            exprs.append(f'MEDIAN(CAST({col_sql} AS DOUBLE)) AS d_{idx}')
            exprs.append(
                f'(QUANTILE_CONT(CAST({col_sql} AS DOUBLE), 0.75) - '
                f'QUANTILE_CONT(CAST({col_sql} AS DOUBLE), 0.25)) AS q_{idx}'
            )
        sql_select = ', '.join(exprs)
        before_row = ctx.connection.execute(
            f'SELECT {sql_select} FROM {table_sql} WHERE {time_sql} <= ?',
            [split_time],
        ).fetchone()
        after_row = ctx.connection.execute(
            f'SELECT {sql_select} FROM {table_sql} WHERE {time_sql} > ?',
            [split_time],
        ).fetchone()

        for idx, col in enumerate(batch):
            offset = idx * 5
            for label, row in (('before', before_row), ('after', after_row)):
                if row is None:
                    continue
                cnt = int(row[offset] or 0)
                stats[col][label] = {
                    'count': cnt,
                    'mean': _finite_or_none(row[offset + 1]),
                    'std': _finite_or_none(row[offset + 2]),
                    'median': _finite_or_none(row[offset + 3]),
                    'iqr': _finite_or_none(row[offset + 4]),
                }
    return stats


def _finite_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return round(num, 6) if math.isfinite(num) else None
    return None


def compare_windows(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Rank numeric columns by robust shift across `split_time`."""
    entry, time_col, err = _require_usable_time_table(ctx, args.get('table'))
    if err is not None or entry is None or time_col is None:
        return err or tables.error_result('internal_error', 'invalid state')

    raw_split = args.get('split_time')
    if not isinstance(raw_split, (int, float)) or isinstance(raw_split, bool):
        return tables.error_result(
            'invalid_args', 'split_time must be a numeric timestamp'
        )
    split_time = float(raw_split)

    raw_top_k = args.get('top_k', 15)
    if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool):
        return tables.error_result('invalid_args', 'top_k must be an integer')
    top_k = max(1, min(raw_top_k, 100))

    cols, col_err = _candidate_numeric_columns(entry, args.get('columns'))
    if col_err is not None or cols is None:
        return col_err or tables.error_result('internal_error', 'bad columns')

    table_sql = time_detect.quote_ident(entry.name)
    time_sql = time_detect.quote_ident(time_col)
    counts = ctx.connection.execute(
        f'SELECT '
        f'COUNT(*) FILTER (WHERE {time_sql} <= ?), '
        f'COUNT(*) FILTER (WHERE {time_sql} > ?) '
        f'FROM {table_sql}',
        [split_time, split_time],
    ).fetchone()
    before_rows = int(counts[0] or 0) if counts else 0
    after_rows = int(counts[1] or 0) if counts else 0
    if before_rows == 0 or after_rows == 0:
        return tables.error_result(
            'empty_window',
            f'Split time {split_time} leaves an empty window '
            f'(before={before_rows}, after={after_rows}); '
            f'valid range is [{entry.time_info.min_time_s}, '
            f'{entry.time_info.max_time_s}].',
        )

    stats = _fetch_window_stats(ctx, entry.name, time_col, cols, split_time)
    ranked: list[dict[str, object]] = []
    for col in cols:
        b = stats[col].get('before', {})
        a = stats[col].get('after', {})
        b_med = b.get('median')
        a_med = a.get('median')
        b_mean = b.get('mean')
        a_mean = a.get('mean')
        if (
            not isinstance(b_med, float)
            or not isinstance(a_med, float)
            or not isinstance(b_mean, float)
            or not isinstance(a_mean, float)
        ):
            continue
        b_iqr = b.get('iqr')
        b_std = b.get('std')
        scale = max(
            float(b_iqr) if isinstance(b_iqr, (int, float)) else 0.0,
            float(b_std) if isinstance(b_std, (int, float)) else 0.0,
            abs(b_med) * 0.05,
            1e-3,
        )
        med_diff = a_med - b_med
        mean_diff = a_mean - b_mean
        med_score = abs(med_diff) / scale
        mean_score = abs(mean_diff) / scale
        score = med_score if med_score > 0.0 else 0.5 * mean_score
        direction = (
            'up'
            if (med_diff > 0 or (med_diff == 0 and mean_diff > 0))
            else ('down' if (med_diff < 0 or mean_diff < 0) else 'flat')
        )
        ranked.append(
            {
                'column': col,
                'score': round(score, 4),
                'direction': direction,
                'before': b,
                'after': a,
            }
        )

    ranked.sort(key=lambda item: float(str(item['score'])), reverse=True)
    top_ranked = ranked[:top_k]

    return {
        'ok': True,
        'table': entry.name,
        'split_time': split_time,
        'window_row_counts': {'before': before_rows, 'after': after_rows},
        'ranked': top_ranked,
        'locator': tool_schema.ToolLocator(
            kind='window_comparison',
            table=entry.name,
            columns=tuple(str(item['column']) for item in top_ranked),
            window=(entry.time_info.min_time_s, entry.time_info.max_time_s),
            expression=f'split_time={split_time}',
        ).to_dict(),
        'target_components': [str(item['column']) for item in top_ranked[:5]],
    }


def time_series(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Downsample one numeric column into at most N time buckets."""
    entry, time_col, err = _require_usable_time_table(ctx, args.get('table'))
    if err is not None or entry is None or time_col is None:
        return err or tables.error_result('internal_error', 'invalid state')

    col_name = args.get('column')
    if not isinstance(col_name, str) or not col_name.strip():
        return tables.error_result(
            'invalid_args', 'column must be a non-empty string'
        )
    cols, col_err = _candidate_numeric_columns(entry, [col_name])
    if col_err is not None or not cols:
        return col_err or tables.error_result('unknown_column', 'bad column')
    target_col = cols[0]

    raw_buckets = args.get('num_buckets', 30)
    if not isinstance(raw_buckets, int) or isinstance(raw_buckets, bool):
        return tables.error_result(
            'invalid_args', 'num_buckets must be an integer'
        )
    num_buckets = max(1, min(raw_buckets, 200))

    agg = str(args.get('agg', 'avg')).lower()
    if agg not in _VALID_AGGS:
        return tables.error_result(
            'invalid_args', f'agg must be one of {sorted(_VALID_AGGS)}'
        )

    t_min = entry.time_info.min_time_s
    t_max = entry.time_info.max_time_s
    start_time = args.get('start_time', t_min)
    end_time = args.get('end_time', t_max)
    if (
        not isinstance(start_time, (int, float))
        or isinstance(start_time, bool)
        or not isinstance(end_time, (int, float))
        or isinstance(end_time, bool)
    ):
        return tables.error_result(
            'invalid_args', 'start_time and end_time must be numeric'
        )
    start_f = float(start_time)
    end_f = float(end_time)
    if end_f < start_f:
        return tables.error_result(
            'invalid_args', 'end_time must be >= start_time'
        )

    span = max(end_f - start_f, 1e-6)
    bucket_width = span / num_buckets
    table_sql = time_detect.quote_ident(entry.name)
    time_sql = time_detect.quote_ident(time_col)
    col_sql = time_detect.quote_ident(target_col)

    rows = ctx.connection.execute(
        f'SELECT '
        f'LEAST({num_buckets - 1}, GREATEST(0, '
        f'CAST(FLOOR(({time_sql} - ?) / ?) AS INTEGER))) AS bucket_idx, '
        f'COUNT({col_sql}), '
        f'MIN(CAST({col_sql} AS DOUBLE)), '
        f'MAX(CAST({col_sql} AS DOUBLE)), '
        f'AVG(CAST({col_sql} AS DOUBLE)), '
        f'SUM(CAST({col_sql} AS DOUBLE)) '
        f'FROM {table_sql} '
        f'WHERE {time_sql} BETWEEN ? AND ? AND {col_sql} IS NOT NULL '
        f'GROUP BY bucket_idx ORDER BY bucket_idx',
        [start_f, bucket_width, start_f, end_f],
    ).fetchall()

    buckets = []
    for idx, cnt, b_min, b_max, b_avg, b_sum in rows:
        b_idx = int(idx)
        agg_values = {
            'avg': _finite_or_none(b_avg),
            'min': _finite_or_none(b_min),
            'max': _finite_or_none(b_max),
            'sum': _finite_or_none(b_sum),
            'count': int(cnt or 0),
        }
        buckets.append(
            {
                'bucket_index': b_idx,
                'bucket_start': round(start_f + b_idx * bucket_width, 3),
                'bucket_end': round(start_f + (b_idx + 1) * bucket_width, 3),
                'value': agg_values[agg],
                'count': int(cnt or 0),
                'min': _finite_or_none(b_min),
                'max': _finite_or_none(b_max),
                'avg': _finite_or_none(b_avg),
            }
        )

    return {
        'ok': True,
        'table': entry.name,
        'column': target_col,
        'agg': agg,
        'num_buckets_requested': num_buckets,
        'buckets': buckets,
        'locator': tool_schema.ToolLocator(
            kind='time_series',
            table=entry.name,
            columns=(target_col,),
            window=(start_f, end_f),
            expression=f'{agg}({target_col})/{num_buckets}_buckets',
        ).to_dict(),
        'target_components': [target_col],
    }


def _column_onset_signal(
    series: Sequence[float | None],
    t_start: float,
    bucket_width: float,
    col_name: str,
    table_name: str,
) -> dict[str, object] | None:
    """Detect the earliest sustained anomaly bucket in a downsampled column."""
    valid_indices = [idx for idx, val in enumerate(series) if val is not None]
    if len(valid_indices) < 6:
        return None
    baseline_count = max(3, len(series) // 5)
    baseline_vals = [
        float(val)
        for val in series[:baseline_count]
        if val is not None and math.isfinite(val)
    ]
    if len(baseline_vals) < 2:
        return None
    b_med = statistics.median(baseline_vals)
    deviations = [abs(v - b_med) for v in baseline_vals]
    mad_scale = 1.4826 * statistics.median(deviations)
    std_scale = (
        statistics.stdev(baseline_vals) if len(baseline_vals) > 1 else 0.0
    )
    scale = max(mad_scale, std_scale, abs(b_med) * 0.05, 1e-3)

    z_scores = [
        (abs(float(val) - b_med) / scale) if val is not None else 0.0
        for val in series
    ]
    peak_z = max(z_scores[baseline_count:] or [0.0])
    if peak_z < 4.0:
        return None

    onset_idx: int | None = None
    for idx in range(baseline_count, len(z_scores)):
        if z_scores[idx] < 3.0:
            continue
        lookahead = z_scores[idx : min(len(z_scores), idx + 3)]
        sustained = sum(1 for val in lookahead if val >= 2.5)
        if sustained >= 2 or idx == len(z_scores) - 1:
            onset_idx = idx
            break

    if onset_idx is None:
        onset_idx = z_scores.index(peak_z)

    onset_time = t_start + onset_idx * bucket_width
    return {
        'table': table_name,
        'column': col_name,
        'signal_type': 'metric_shift',
        'onset_time_s': round(onset_time, 3),
        'peak_z_score': round(peak_z, 2),
        'baseline_median': round(b_med, 6),
    }


def _scan_table_onset_signals(
    ctx: tables.ToolContext,
    entry: ingest_schema.TableCatalogEntry,
    num_buckets: int,
) -> list[dict[str, object]]:
    """Find metric change points and error-rate bursts inside one table."""
    time_col = entry.time_info.normalised_column
    t_min = entry.time_info.min_time_s
    t_max = entry.time_info.max_time_s
    if (
        time_col is None
        or t_min is None
        or t_max is None
        or entry.row_count < 6
    ):
        return []
    span = max(t_max - t_min, 1e-6)
    bucket_width = span / num_buckets
    table_sql = time_detect.quote_ident(entry.name)
    time_sql = time_detect.quote_ident(time_col)
    signals: list[dict[str, object]] = []

    if entry.kind == 'tabular':
        cols, _ = _candidate_numeric_columns(entry, None)
        if cols:
            cols_to_scan = cols[:80]
            series_by_col: dict[str, list[float | None]] = {
                col: [None] * num_buckets for col in cols_to_scan
            }
            for start in range(0, len(cols_to_scan), 20):
                batch = cols_to_scan[start : start + 20]
                aggs = ', '.join(
                    f'AVG(CAST({time_detect.quote_ident(c)} AS DOUBLE))'
                    for c in batch
                )
                rows = ctx.connection.execute(
                    f'SELECT '
                    f'LEAST({num_buckets - 1}, GREATEST(0, '
                    f'CAST(FLOOR(({time_sql} - ?) / ?) AS INTEGER))) AS b, '
                    f'{aggs} '
                    f'FROM {table_sql} WHERE {time_sql} IS NOT NULL '
                    f'GROUP BY b ORDER BY b',
                    [t_min, bucket_width],
                ).fetchall()
                for row in rows:
                    b_idx = int(row[0])
                    for offset, col in enumerate(batch):
                        val = row[offset + 1]
                        if isinstance(val, (int, float)) and math.isfinite(
                            float(val)
                        ):
                            series_by_col[col][b_idx] = float(val)

            for col in cols_to_scan:
                sig = _column_onset_signal(
                    series_by_col[col], t_min, bucket_width, col, entry.name
                )
                if sig is not None:
                    signals.append(sig)

    col_names = {col.name.lower(): col.name for col in entry.columns}
    level_col = col_names.get('level')
    error_col = col_names.get('error')
    if level_col is not None or error_col is not None:
        predicates: list[str] = []
        if level_col is not None:
            l_sql = time_detect.quote_ident(level_col)
            predicates.append(
                f"UPPER(COALESCE(CAST({l_sql} AS VARCHAR), '')) "
                "IN ('ERROR', 'ERR', 'FATAL', 'CRITICAL', 'CRIT')"
            )
        if error_col is not None:
            e_sql = time_detect.quote_ident(error_col)
            predicates.append(
                f"NULLIF(TRIM(CAST({e_sql} AS VARCHAR)), '') IS NOT NULL "
                f'AND LOWER(TRIM(CAST({e_sql} AS VARCHAR))) '
                f"NOT IN ('0', 'false')"
            )
        where_err = ' OR '.join(predicates)
        err_counts = [0.0] * num_buckets
        rows = ctx.connection.execute(
            f'SELECT '
            f'LEAST({num_buckets - 1}, GREATEST(0, '
            f'CAST(FLOOR(({time_sql} - ?) / ?) AS INTEGER))) AS b, '
            f'COUNT(*) FILTER (WHERE {where_err}) '
            f'FROM {table_sql} WHERE {time_sql} IS NOT NULL '
            f'GROUP BY b ORDER BY b',
            [t_min, bucket_width],
        ).fetchall()
        for b_idx, cnt in rows:
            err_counts[int(b_idx)] = float(cnt or 0)
        baseline_count = max(3, num_buckets // 5)
        base = err_counts[:baseline_count]
        base_mean = statistics.mean(base)
        base_std = statistics.stdev(base) if len(base) > 1 else 0.0
        threshold = max(3.0, base_mean + 4.0 * max(base_std, 1.0))
        for idx in range(baseline_count, num_buckets):
            if err_counts[idx] >= threshold:
                signals.append(
                    {
                        'table': entry.name,
                        'column': level_col or error_col,
                        'signal_type': 'error_burst',
                        'onset_time_s': round(t_min + idx * bucket_width, 3),
                        'peak_z_score': round(
                            (err_counts[idx] - base_mean) / max(base_std, 1.0),
                            2,
                        ),
                        'baseline_median': round(base_mean, 3),
                    }
                )
                break

    return signals


def find_onset(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Estimate when an incident started from metric and error change points."""
    raw_tables = args.get('tables')
    if raw_tables is not None and (
        not isinstance(raw_tables, list)
        or not all(isinstance(t, str) for t in raw_tables)
    ):
        return tables.error_result(
            'invalid_args', 'tables must be a list of table names'
        )

    raw_buckets = args.get('num_buckets', 60)
    if not isinstance(raw_buckets, int) or isinstance(raw_buckets, bool):
        return tables.error_result(
            'invalid_args', 'num_buckets must be an integer'
        )
    num_buckets = max(10, min(raw_buckets, 180))

    target_entries: list[ingest_schema.TableCatalogEntry] = []
    if raw_tables is not None:
        for name in raw_tables:
            entry = ctx.catalog.get_table(name)
            if entry is None:
                return tables.error_result(
                    'unknown_table', f'Table not found in catalog: {name}'
                )
            if not entry.time_info.time_usable:
                return tables.error_result(
                    'unusable_time',
                    f'Table {entry.name!r} has no usable timestamp mapping.',
                )
            target_entries.append(entry)
    else:
        target_entries = [
            t for t in ctx.catalog.tables if t.time_info.time_usable
        ]

    if not target_entries:
        return tables.error_result(
            'unusable_time',
            'No tables in catalog have usable timestamps for onset detection.',
        )

    all_signals: list[dict[str, object]] = []
    for entry in target_entries:
        all_signals.extend(
            _scan_table_onset_signals(ctx, entry, num_buckets=num_buckets)
        )

    if not all_signals:
        return {
            'ok': True,
            'onset_detected': False,
            'estimated_onset_time_s': None,
            'confidence': 'low',
            'message': 'No strong metric shift or error burst detected.',
            'signals': [],
            'locator': tool_schema.ToolLocator(
                kind='onset_estimate',
                columns=(),
                expression='no_change_point',
            ).to_dict(),
            'target_components': [],
        }

    all_signals.sort(
        key=lambda sig: float(str(sig['peak_z_score'])), reverse=True
    )
    top_signals = all_signals[:10]
    onset_times = [float(str(sig['onset_time_s'])) for sig in top_signals]
    estimated_onset = round(statistics.median(onset_times), 3)
    peak_z = float(str(top_signals[0]['peak_z_score']))
    confidence = 'high' if len(top_signals) >= 2 and peak_z >= 8.0 else 'medium'

    return {
        'ok': True,
        'onset_detected': True,
        'estimated_onset_time_s': estimated_onset,
        'confidence': confidence,
        'signals': top_signals,
        'locator': tool_schema.ToolLocator(
            kind='onset_estimate',
            table=str(top_signals[0]['table']),
            columns=tuple(
                str(sig['column'])
                for sig in top_signals
                if sig['column'] is not None
            ),
            window=(estimated_onset, estimated_onset),
            expression=f'consensus_median_of_{len(top_signals)}_signals',
        ).to_dict(),
        'target_components': list(
            dict.fromkeys(
                str(sig['column'])
                for sig in top_signals[:5]
                if sig['column'] is not None
            )
        ),
    }
