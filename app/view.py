"""Pure view-model builders and formatters for the Streamlit upload app."""

import contextlib
import dataclasses
import datetime
import math
import pathlib
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Literal

import duckdb

from incidents_agent.agent import ledger
from incidents_agent.agent import render
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.ingest import loaders
from incidents_agent.ingest import time_detect

type StateKind = Literal[
    'no_usable_model',
    'budget_exhausted',
    'run_error',
    'no_fault_found',
    'inconclusive',
    'provisional',
    'verified',
]
type BannerLevel = Literal['error', 'warning', 'info', 'success']

_RISK_LABELS: dict[report.Risk, str] = {
    'read_only': 'Read-only check',
    'reversible': 'Reversible change',
    'disruptive': 'Disruptive change',
}
_OUTCOME_TEXT: dict[report.Outcome, str] = {
    'verified': (
        'Verified: the top cause survived a check that could have disproved it.'
    ),
    'provisional': (
        'Provisional: a ranking exists, but the top cause was not confirmed '
        'by a disproof check.'
    ),
    'inconclusive': ('Inconclusive: no cause could be supported by the data.'),
    'no_fault_found': (
        'No fault found: the data was examined and nothing abnormal was found.'
    ),
}
_BUDGET_STOPS = frozenset({'spend_refused', 'cost_cap', 'reservation_refused'})
_NUMERIC_TYPES = ('INT', 'DOUBLE', 'FLOAT', 'DECIMAL', 'NUMERIC')


@dataclasses.dataclass(frozen=True)
class StateBanner:
    """Top-level status banner shown in the UI.

    Attributes:
        kind: Categorised state identifier.
        level: Streamlit alert severity level.
        title: Short summary heading.
        detail: Actionable explanation for the user.
    """

    kind: StateKind
    level: BannerLevel
    title: str
    detail: str


@dataclasses.dataclass(frozen=True)
class DocumentExcerpt:
    """Cited document chunk and heading context extracted from tool output.

    Attributes:
        doc_id: Document identifier (relative path).
        chunk_id: Unique chunk identifier.
        heading_path: Heading or section breadcrumb for the chunk.
        text: Chunk content or excerpt.
    """

    doc_id: str
    chunk_id: str
    heading_path: str
    text: str


@dataclasses.dataclass(frozen=True)
class LinkedEvidence:
    """Evidence citation linked to its underlying data-tool call.

    Attributes:
        call_id: Tool call identifier.
        tool: Tool name that produced the result.
        args: Arguments passed to the tool call.
        locator: Locator string inside the tool result.
        quote: Quoted phrase or value.
        stance: Whether the evidence supports or opposes the hypothesis.
        valid: Whether citation validation succeeded against the trajectory.
        document_chunk: Extracted chunk details for document tools.
    """

    call_id: str
    tool: str
    args: Mapping[str, object]
    locator: str
    quote: str
    stance: Literal['for', 'against']
    valid: bool
    document_chunk: DocumentExcerpt | None = None


@dataclasses.dataclass(frozen=True)
class MetricPoint:
    """One plotted point in a cited metric chart.

    Attributes:
        timestamp_s: Unix seconds timestamp.
        label: Human-readable axis label.
        value: Numeric metric value.
    """

    timestamp_s: float
    label: str
    value: float


@dataclasses.dataclass(frozen=True)
class MetricChart:
    """Plottable chart series for a metric cited as evidence.

    Attributes:
        call_id: Tool call identifier that led to this metric.
        table: DuckDB table name.
        column: Numeric column name.
        split_time_s: Incident start or split time when known.
        points: Ordered points to plot.
        kind: Whether the series is a downsampled time series or window pair.
    """

    call_id: str
    table: str
    column: str
    split_time_s: float | None
    points: tuple[MetricPoint, ...]
    kind: Literal['time_series', 'window_comparison']


@dataclasses.dataclass(frozen=True)
class ActionView:
    """Recommended action prepared for display.

    Attributes:
        hypothesis_id: Target hypothesis id or component name.
        action: Action text.
        risk: Typed risk category.
        risk_label: Human-readable risk label.
        how_to_verify: Verification instruction.
    """

    hypothesis_id: str
    action: str
    risk: report.Risk
    risk_label: str
    how_to_verify: str


@dataclasses.dataclass(frozen=True)
class DataRequestView:
    """Data request linked to the open/undecided hypothesis it would settle.

    Attributes:
        hypothesis_id: Target hypothesis identifier.
        component: Suspected component from the ledger when available.
        hypothesis_statement: Statement of the hypothesis when available.
        what: Requested data description.
        source: Where the data comes from.
        effect: How the data would confirm or refute the hypothesis.
    """

    hypothesis_id: str
    component: str
    hypothesis_statement: str
    what: str
    source: str
    effect: str


@dataclasses.dataclass(frozen=True)
class EstimatedOnsetView:
    """Onset estimate produced by `find_onset` when incident time was omitted.

    Attributes:
        call_id: Tool call identifier for `find_onset`.
        estimated_onset_time_s: Estimated start time in Unix seconds.
        confidence: Confidence string returned by `find_onset`.
        top_signal: Short description of the strongest detected signal.
    """

    call_id: str
    estimated_onset_time_s: float
    confidence: str
    top_signal: str


@dataclasses.dataclass(frozen=True)
class CauseView:
    """Ranked root-cause candidate with linked evidence items.

    Attributes:
        rank: Rank position (1 = top).
        component: Component or service name.
        statement: Hypothesis statement.
        hypothesis_id: Ledger hypothesis id, or None when ledger was off.
        verification: Disproof check outcome.
        confidence: Qualitative confidence level.
        confidence_reasons: Reasons for that confidence level.
        cited: Whether valid current-data evidence backs this cause.
        evidence: Linked evidence items.
    """

    rank: int
    component: str
    statement: str
    hypothesis_id: str | None
    verification: report.Verification
    confidence: report.Confidence
    confidence_reasons: tuple[str, ...]
    cited: bool
    evidence: tuple[LinkedEvidence, ...]


@dataclasses.dataclass(frozen=True)
class ReportViewModel:
    """Complete view model for rendering an incident report outside Streamlit.

    Attributes:
        run_id: Run identifier.
        continuation: Continuation pass index.
        outcome: Final report outcome.
        outcome_text: Human-readable outcome description.
        stop_reason: Why the run ended.
        fault_type: Optional fault category.
        banner: Top-level status banner for this run.
        causes: Ranked causes with linked evidence.
        actions: Recommended actions ordered read-only first.
        data_requests: Hypothesis-linked data requests when unsettled.
        dropped: Items removed by validation.
        charts: Metric charts for cited numeric observations.
        estimated_onset: Estimated onset from `find_onset` when unprovided.
        valid_citations: Number of citations that pass validation.
        total_citations: Total citations in ranked causes.
        tool_calls: Total data tool calls used.
        model_turns: Total model calls made.
        cost_usd: Settled model cost string.
        elapsed_s: Wall-clock duration in seconds.
        prompt_hash: Prompt module hash.
        markdown: Deterministic Markdown export matching `scripts/replay.py`.
    """

    run_id: str
    continuation: int
    outcome: report.Outcome
    outcome_text: str
    stop_reason: str
    fault_type: str | None
    banner: StateBanner
    causes: tuple[CauseView, ...]
    actions: tuple[ActionView, ...]
    data_requests: tuple[DataRequestView, ...]
    dropped: tuple[str, ...]
    charts: tuple[MetricChart, ...]
    estimated_onset: EstimatedOnsetView | None
    valid_citations: int
    total_citations: int
    tool_calls: int
    model_turns: int
    cost_usd: str
    elapsed_s: float
    prompt_hash: str
    markdown: str


@dataclasses.dataclass(frozen=True)
class LiveToolCall:
    """One recorded tool call formatted for the live trajectory view.

    Attributes:
        seq: Trajectory sequence number.
        call_id: Run-scoped call identifier.
        continuation: Continuation pass index.
        role: Role tag (`agent` in single mode).
        node: Graph node name.
        tool: Tool name.
        args: Tool call arguments.
        ok: Whether the tool succeeded.
        locator: Returned locator mapping, if any.
        target_components: Target components returned by the tool.
    """

    seq: int
    call_id: str
    continuation: int
    role: str
    node: str
    tool: str
    args: Mapping[str, object]
    ok: bool
    locator: Mapping[str, object] | None
    target_components: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class LiveHypothesis:
    """Current ledger entry formatted for the live trajectory view.

    Attributes:
        id: Hypothesis identifier (`H1`, ...).
        component: Suspected component.
        statement: Hypothesis statement.
        status: `open`, `supported`, or `rejected`.
        confidence: Qualitative confidence.
        verification: Verification outcome.
        evidence_for: Count of supporting citations.
        evidence_against: Count of contrary citations.
        origin: `data` or `memory`.
    """

    id: str
    component: str
    statement: str
    status: str
    confidence: str
    verification: str
    evidence_for: int
    evidence_against: int
    origin: str


@dataclasses.dataclass(frozen=True)
class RoleLane:
    """Timeline items grouped by role tag (`agent` in single mode).

    Attributes:
        role: Role identifier.
        tool_calls: Tool calls emitted by this role.
        model_turns: Count of model calls made by this role.
    """

    role: str
    tool_calls: tuple[LiveToolCall, ...]
    model_turns: int


@dataclasses.dataclass(frozen=True)
class LiveViewModel:
    """Live view state built incrementally from trajectory events.

    Attributes:
        active_node: Latest entered graph node, if any.
        tool_calls: Total tool calls seen so far.
        model_turns: Total model turns seen so far.
        cost_usd: Latest cumulative settled cost string.
        elapsed_s: Latest recorded wall-clock seconds.
        lanes: Role-keyed lanes of tool calls and turns.
        hypotheses: Latest hypothesis ledger snapshot.
        errors: Model error summaries observed in the trajectory.
    """

    active_node: str | None
    tool_calls: int
    model_turns: int
    cost_usd: str
    elapsed_s: float
    lanes: Mapping[str, RoleLane]
    hypotheses: tuple[LiveHypothesis, ...]
    errors: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ContinuationDiffView:
    """Summary of what changed between two passes of the same run.

    Attributes:
        previous_continuation: Earlier continuation index.
        current_continuation: Newer continuation index.
        added_files: File names added for the newer pass.
        catalog_changes: Catalog diff lines recorded on continuation.
        previous_outcome: Outcome before continuation.
        current_outcome: Outcome after continuation.
        previous_ranks: Ranks before continuation.
        current_ranks: Ranks after continuation.
        verification_changes: Human-readable verification transitions.
        new_evidence_calls: Call ids cited in the new report that were not in
            the previous report.
    """

    previous_continuation: int
    current_continuation: int
    added_files: tuple[str, ...]
    catalog_changes: tuple[str, ...]
    previous_outcome: report.Outcome
    current_outcome: report.Outcome
    previous_ranks: tuple[str, ...]
    current_ranks: tuple[str, ...]
    verification_changes: tuple[str, ...]
    new_evidence_calls: tuple[str, ...]


def determine_state_banner(
    *,
    has_usable_model: bool,
    use_fake: bool,
    budget_exhausted: bool,
    run_error: str | None = None,
    final: report.Report | None = None,
    events: Sequence[Mapping[str, object]] = (),
) -> StateBanner:
    """Return the clear UI status banner for pre-run or post-run states.

    Args:
        has_usable_model: Whether at least one configured model is usable.
        use_fake: Whether the zero-cost rule-based fake client is selected.
        budget_exhausted: Whether the global spend ledger has no balance left
            or is halted pending reconciliation.
        run_error: Unexpected runtime exception message, if one occurred.
        final: Finished report when a run completed or was loaded.
        events: Trajectory events for detecting `model_error` records.

    Returns:
        Structured banner describing the state.
    """
    model_errors = [
        str(event.get('message') or event.get('reason') or 'model error')
        for event in events
        if event.get('type') == 'model_error'
    ]
    if run_error:
        return StateBanner(
            kind='run_error',
            level='error',
            title='Run failed',
            detail=run_error,
        )
    if final is not None and (
        final.stop_reason == 'model_error' or model_errors
    ):
        reason = model_errors[-1] if model_errors else final.stop_reason
        return StateBanner(
            kind='run_error',
            level='error',
            title='Run interrupted by model error',
            detail=(
                f'The model call failed ({reason}). Showing whatever '
                'evidence was finalised from the ledger.'
            ),
        )
    if final is not None and final.stop_reason in _BUDGET_STOPS:
        return StateBanner(
            kind='budget_exhausted',
            level='warning',
            title='Budget or cost cap reached',
            detail=(
                f'Run stopped due to `{final.stop_reason}`. Any validated '
                'findings collected before stopping are shown below.'
            ),
        )
    if budget_exhausted and final is None and not use_fake:
        return StateBanner(
            kind='budget_exhausted',
            level='error',
            title='Budget exhausted or halted',
            detail=(
                'The global spend guard has no remaining balance or is halted '
                'pending reconciliation. Use free demo mode or inspect past '
                'runs.'
            ),
        )
    if not has_usable_model and not use_fake and final is None:
        return StateBanner(
            kind='no_usable_model',
            level='warning',
            title='No usable provider model configured',
            detail=(
                'Configure an API key in `.env` and a valid pinned model in '
                '`config/models.yaml`, or select the free demo mode (`--fake`).'
            ),
        )
    if final is None:
        return StateBanner(
            kind='verified',
            level='info',
            title='Ready to investigate',
            detail='Stage files or pick a case, review ingest, and run.',
        )
    if final.outcome == 'no_fault_found':
        return StateBanner(
            kind='no_fault_found',
            level='success',
            title='No fault found',
            detail=_OUTCOME_TEXT['no_fault_found'],
        )
    if final.outcome == 'inconclusive':
        return StateBanner(
            kind='inconclusive',
            level='warning',
            title='Inconclusive diagnosis',
            detail=(
                'No cause could be supported by current data. Review the '
                'data requests below and add files to continue.'
            ),
        )
    if final.outcome == 'provisional':
        return StateBanner(
            kind='provisional',
            level='info',
            title='Provisional diagnosis',
            detail=_OUTCOME_TEXT['provisional'],
        )
    return StateBanner(
        kind='verified',
        level='success',
        title='Verified diagnosis',
        detail=_OUTCOME_TEXT['verified'],
    )


def _norm(text: str) -> str:
    return ' '.join(text.casefold().split())


def _extract_document_chunk(
    stored: results.StoredResult | None, ref: report.EvidenceRef
) -> DocumentExcerpt | None:
    if stored is None or stored.tool not in (
        'read_document',
        'search_documents',
    ):
        return None
    body = results.payload(stored.result)
    content = body.get('content')
    if not isinstance(content, Mapping):
        return None
    quote_norm = _norm(ref.quote)

    chunks_raw = content.get('chunks')
    if isinstance(chunks_raw, list):
        candidates = [c for c in chunks_raw if isinstance(c, Mapping)]
        chosen = _pick_chunk_candidate(candidates, ref.locator, quote_norm)
        if chosen is not None:
            return DocumentExcerpt(
                doc_id=str(chosen.get('doc_id', '')),
                chunk_id=str(chosen.get('chunk_id', '')),
                heading_path=str(chosen.get('heading_path', '')),
                text=str(chosen.get('text', '')),
            )

    results_raw = content.get('results')
    if isinstance(results_raw, list):
        candidates = [c for c in results_raw if isinstance(c, Mapping)]
        chosen = _pick_chunk_candidate(candidates, ref.locator, quote_norm)
        if chosen is not None:
            return DocumentExcerpt(
                doc_id=str(chosen.get('doc_id', '')),
                chunk_id=str(chosen.get('chunk_id', '')),
                heading_path=str(chosen.get('heading_path', '')),
                text=str(chosen.get('excerpt', '')),
            )

    outline_raw = content.get('outline')
    if isinstance(outline_raw, list):
        candidates = [c for c in outline_raw if isinstance(c, Mapping)]
        chosen = _pick_chunk_candidate(candidates, ref.locator, quote_norm)
        if chosen is not None:
            return DocumentExcerpt(
                doc_id=str(content.get('doc_id', '')),
                chunk_id=str(chosen.get('chunk_id', '')),
                heading_path=str(chosen.get('heading_path', '')),
                text='',
            )
    return None


def _pick_chunk_candidate(
    candidates: Sequence[Mapping[str, object]],
    locator: str,
    quote_norm: str,
) -> Mapping[str, object] | None:
    for item in candidates:
        if str(item.get('chunk_id', '')) == locator:
            return item
    if quote_norm:
        for item in candidates:
            haystack = _norm(
                f'{item.get("heading_path", "")} '
                f'{item.get("text", item.get("excerpt", ""))}'
            )
            if quote_norm in haystack:
                return item
    for item in candidates:
        if item.get('is_target') is True:
            return item
    return candidates[0] if candidates else None


def _format_ts(ts: float) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
        return dt.strftime('%H:%M:%S')
    except (OSError, OverflowError, ValueError):
        return f'{ts:.1f}'


def _points_from_buckets(buckets: object) -> tuple[MetricPoint, ...]:
    if not isinstance(buckets, list):
        return ()
    points: list[MetricPoint] = []
    for item in buckets:
        if not isinstance(item, Mapping):
            continue
        start = item.get('bucket_start')
        val = item.get('value')
        if (
            isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(val, (int, float))
            and not isinstance(val, bool)
            and math.isfinite(float(val))
        ):
            ts = float(start)
            points.append(
                MetricPoint(
                    timestamp_s=ts,
                    label=_format_ts(ts),
                    value=float(val),
                )
            )
    return tuple(points)


def _query_duckdb_series(
    db_path: pathlib.Path,
    table: str,
    column: str,
    *,
    num_buckets: int = 30,
) -> tuple[MetricPoint, ...]:
    if not db_path.is_file():
        return ()
    conn: duckdb.DuckDBPyConnection | None = None
    try:
        conn = loaders.open_readonly_connection(db_path)
        table_sql = time_detect.quote_ident(table)
        desc = conn.execute(f'DESCRIBE {table_sql}').fetchall()
        col_types = {str(row[0]): str(row[1]).upper() for row in desc}
        if '_time_unix_s' not in col_types or column not in col_types:
            return ()
        dtype = col_types[column]
        if not any(tok in dtype for tok in _NUMERIC_TYPES):
            return ()
        col_sql = time_detect.quote_ident(column)
        bounds = conn.execute(
            f'SELECT MIN(_time_unix_s), MAX(_time_unix_s) '
            f'FROM {table_sql} WHERE _time_unix_s IS NOT NULL'
        ).fetchone()
        if not bounds or bounds[0] is None or bounds[1] is None:
            return ()
        t_min = float(bounds[0])
        t_max = float(bounds[1])
        span = max(t_max - t_min, 1e-6)
        width = span / num_buckets
        rows = conn.execute(
            f'SELECT '
            f'LEAST({num_buckets - 1}, GREATEST(0, '
            f'CAST(FLOOR((_time_unix_s - ?) / ?) AS INTEGER))) AS b, '
            f'AVG(CAST({col_sql} AS DOUBLE)) '
            f'FROM {table_sql} '
            f'WHERE _time_unix_s BETWEEN ? AND ? AND {col_sql} IS NOT NULL '
            f'GROUP BY b ORDER BY b',
            [t_min, width, t_min, t_max],
        ).fetchall()
        points: list[MetricPoint] = []
        for idx, val in rows:
            if isinstance(val, (int, float)) and math.isfinite(float(val)):
                ts = round(t_min + int(idx) * width, 3)
                points.append(
                    MetricPoint(
                        timestamp_s=ts,
                        label=_format_ts(ts),
                        value=float(val),
                    )
                )
        return tuple(points)
    except duckdb.Error:
        return ()
    finally:
        if conn is not None:
            with contextlib.suppress(duckdb.Error):
                conn.close()


def _resolve_compare_column(
    stored: results.StoredResult, ref: report.EvidenceRef
) -> tuple[str, dict[str, object] | None]:
    ranked = stored.result.get('ranked')
    if not isinstance(ranked, list):
        return '', None
    items = [item for item in ranked if isinstance(item, dict)]
    if not items:
        return '', None
    table = str(stored.result.get('table', ''))
    locator_col = ref.locator
    if table and locator_col.startswith(f'{table}.'):
        locator_col = locator_col[len(table) + 1 :]
    for item in items:
        if str(item.get('column', '')) == locator_col:
            return str(item['column']), item
    quote_cf = ref.quote.casefold()
    for item in items:
        col_name = str(item.get('column', ''))
        if col_name and col_name.casefold() in quote_cf:
            return col_name, item
    return str(items[0].get('column', '')), items[0]


def _chart_from_citation(
    stored: results.StoredResult,
    ref: report.EvidenceRef,
    store: results.ResultStore,
    db_path: pathlib.Path | None,
    split_hint: float | None,
) -> MetricChart | None:
    if not stored.ok:
        return None
    if stored.tool == 'time_series':
        table = str(stored.result.get('table', ''))
        column = str(stored.result.get('column', ''))
        points = _points_from_buckets(stored.result.get('buckets'))
        if table and column and points:
            return MetricChart(
                call_id=stored.call_id,
                table=table,
                column=column,
                split_time_s=split_hint,
                points=points,
                kind='time_series',
            )
        return None

    if stored.tool == 'compare_windows':
        table = str(stored.result.get('table', ''))
        column, entry = _resolve_compare_column(stored, ref)
        raw_split = stored.result.get('split_time')
        split_s = (
            float(raw_split)
            if isinstance(raw_split, (int, float))
            and not isinstance(raw_split, bool)
            else split_hint
        )
        if not table or not column:
            return None
        points = _series_for_pair(table, column, store, db_path)
        if points:
            return MetricChart(
                call_id=stored.call_id,
                table=table,
                column=column,
                split_time_s=split_s,
                points=points,
                kind='time_series',
            )
        if entry is not None:
            before = entry.get('before')
            after = entry.get('after')
            b_med = (
                before.get('median') if isinstance(before, Mapping) else None
            )
            a_med = after.get('median') if isinstance(after, Mapping) else None
            if isinstance(b_med, (int, float)) and isinstance(
                a_med, (int, float)
            ):
                base_ts = split_s if split_s is not None else 0.0
                return MetricChart(
                    call_id=stored.call_id,
                    table=table,
                    column=column,
                    split_time_s=split_s,
                    points=(
                        MetricPoint(
                            base_ts - 1.0, 'Before (median)', float(b_med)
                        ),
                        MetricPoint(
                            base_ts + 1.0, 'After (median)', float(a_med)
                        ),
                    ),
                    kind='window_comparison',
                )
        return None

    if stored.tool == 'find_onset':
        signals = stored.result.get('signals')
        if not isinstance(signals, list):
            return None
        raw_onset = stored.result.get('estimated_onset_time_s')
        onset_s = (
            float(raw_onset)
            if isinstance(raw_onset, (int, float))
            and not isinstance(raw_onset, bool)
            else split_hint
        )
        for item in signals:
            if not isinstance(item, Mapping):
                continue
            table = str(item.get('table', ''))
            column = str(item.get('column', ''))
            if not table or not column:
                continue
            if ref.locator in (table, column, f'{table}.{column}') or (
                column.casefold() in ref.quote.casefold()
            ):
                points = _series_for_pair(table, column, store, db_path)
                if points:
                    return MetricChart(
                        call_id=stored.call_id,
                        table=table,
                        column=column,
                        split_time_s=onset_s,
                        points=points,
                        kind='time_series',
                    )
        return None

    if stored.tool == 'run_sql' and db_path is not None:
        locator = stored.locator or {}
        table = str(locator.get('table', ''))
        columns = locator.get('columns')
        if table and isinstance(columns, list):
            for raw_col in columns:
                col_name = str(raw_col)
                if col_name == '_time_unix_s':
                    continue
                points = _query_duckdb_series(db_path, table, col_name)
                if points:
                    return MetricChart(
                        call_id=stored.call_id,
                        table=table,
                        column=col_name,
                        split_time_s=split_hint,
                        points=points,
                        kind='time_series',
                    )
    return None


def _series_for_pair(
    table: str,
    column: str,
    store: results.ResultStore,
    db_path: pathlib.Path | None,
) -> tuple[MetricPoint, ...]:
    for item in store:
        if (
            item.ok
            and item.tool == 'time_series'
            and str(item.result.get('table', '')) == table
            and str(item.result.get('column', '')) == column
        ):
            points = _points_from_buckets(item.result.get('buckets'))
            if points:
                return points
    if db_path is not None:
        return _query_duckdb_series(db_path, table, column)
    return ()


def extract_cited_charts(
    final: report.Report,
    store: results.ResultStore,
    *,
    db_path: pathlib.Path | None = None,
    split_hint: float | None = None,
) -> tuple[MetricChart, ...]:
    """Build metric chart series for metrics cited in the report's causes.

    Args:
        final: Final report.
        store: Stored data-tool results from the trajectory.
        db_path: Optional read-only DuckDB path to query full time series.
        split_hint: Optional incident split or onset timestamp in Unix seconds.

    Returns:
        Deduplicated metric charts in cause/evidence order.
    """
    charts: list[MetricChart] = []
    seen: set[tuple[str, str]] = set()
    for cause in final.causes:
        for ref in cause.evidence:
            stored = store.get(ref.call_id)
            if stored is None:
                continue
            chart = _chart_from_citation(
                stored, ref, store, db_path, split_hint
            )
            if chart is None:
                continue
            key = (chart.table, chart.column)
            if key not in seen:
                seen.add(key)
                charts.append(chart)
    return tuple(charts)


def extract_estimated_onset(
    events: Sequence[Mapping[str, object]],
) -> EstimatedOnsetView | None:
    """Return the latest `find_onset` estimate from trajectory events, if any.

    Args:
        events: Trajectory events in order.

    Returns:
        Estimated onset details, or None if no onset was detected.
    """
    latest: EstimatedOnsetView | None = None
    for event in events:
        if (
            event.get('type') != 'tool_call'
            or event.get('tool') != 'find_onset'
        ):
            continue
        if event.get('ok') is not True:
            continue
        result = event.get('result')
        if (
            not isinstance(result, Mapping)
            or result.get('onset_detected') is not True
        ):
            continue
        raw_onset = result.get('estimated_onset_time_s')
        if not isinstance(raw_onset, (int, float)) or isinstance(
            raw_onset, bool
        ):
            continue
        signals = result.get('signals')
        top_signal = ''
        if (
            isinstance(signals, list)
            and signals
            and isinstance(signals[0], Mapping)
        ):
            sig = signals[0]
            sig_type = sig.get('signal_type', '')
            z_score = sig.get('peak_z_score', '')
            top_signal = (
                f'{sig.get("table", "")}.{sig.get("column", "")} '
                f'({sig_type}, z={z_score})'
            )
        latest = EstimatedOnsetView(
            call_id=str(event.get('call_id', '')),
            estimated_onset_time_s=float(raw_onset),
            confidence=str(result.get('confidence', 'low')),
            top_signal=top_signal.strip(),
        )
    return latest


def _latest_ledger_entries(
    events: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    entries: dict[str, Mapping[str, object]] = {}
    for event in events:
        if event.get('type') != 'ledger':
            continue
        book = event.get('ledger')
        if not isinstance(book, Mapping):
            continue
        raw_entries = book.get('entries')
        if not isinstance(raw_entries, list):
            continue
        entries = {
            str(item['id']): item
            for item in raw_entries
            if isinstance(item, Mapping) and 'id' in item
        }
    return entries


def build_report_view(
    final: report.Report,
    events: Sequence[Mapping[str, object]],
    *,
    db_path: pathlib.Path | None = None,
    task_fault_start_s: float | None = None,
) -> ReportViewModel:
    """Build a complete, testable view model from a report and trajectory.

    Args:
        final: Final report to render.
        events: Trajectory events for this run.
        db_path: Optional DuckDB file path for expanding cited metric charts.
        task_fault_start_s: Known fault start time when supplied by the user.

    Returns:
        Structured report view model ready for Streamlit or unit tests.
    """
    store = results.ResultStore()
    for event in events:
        if (
            event.get('type') == 'tool_call'
            and event.get('call_id') not in store
        ):
            store.add(results.StoredResult.from_dict(event))

    estimated_onset = (
        extract_estimated_onset(events) if task_fault_start_s is None else None
    )
    split_hint = (
        task_fault_start_s
        if task_fault_start_s is not None
        else (
            estimated_onset.estimated_onset_time_s
            if estimated_onset is not None
            else None
        )
    )

    valid_citations = 0
    total_citations = 0
    causes_out: list[CauseView] = []
    for cause in final.causes:
        linked: list[LinkedEvidence] = []
        for ref in cause.evidence:
            total_citations += 1
            checked, _ = ledger.check_citation(
                ref.to_dict(),
                store,
                final.run_id,
                default_stance=ref.stance,
            )
            is_valid = checked is not None
            if is_valid:
                valid_citations += 1
            stored = store.get(ref.call_id)
            linked.append(
                LinkedEvidence(
                    call_id=ref.call_id,
                    tool=stored.tool if stored is not None else 'unknown',
                    args=dict(stored.args) if stored is not None else {},
                    locator=ref.locator,
                    quote=ref.quote,
                    stance=ref.stance,
                    valid=is_valid,
                    document_chunk=_extract_document_chunk(stored, ref),
                )
            )
        causes_out.append(
            CauseView(
                rank=cause.rank,
                component=cause.component,
                statement=cause.statement,
                hypothesis_id=cause.hypothesis_id,
                verification=cause.verification,
                confidence=cause.confidence,
                confidence_reasons=cause.confidence_reasons,
                cited=cause.cited,
                evidence=tuple(linked),
            )
        )

    sorted_actions = sorted(
        final.actions,
        key=lambda item: report.RISK_ORDER.index(item.risk),
    )
    action_views = tuple(
        ActionView(
            hypothesis_id=item.hypothesis_id,
            action=item.action,
            risk=item.risk,
            risk_label=_RISK_LABELS[item.risk],
            how_to_verify=item.how_to_verify,
        )
        for item in sorted_actions
    )

    ledger_map = _latest_ledger_entries(events)
    cause_by_hid = {
        cause.hypothesis_id: cause
        for cause in final.causes
        if cause.hypothesis_id
    }
    request_views: list[DataRequestView] = []
    if final.outcome in ('provisional', 'inconclusive'):
        for req in final.data_requests:
            entry = ledger_map.get(req.hypothesis_id)
            ranked_cause = cause_by_hid.get(req.hypothesis_id)
            component = (
                str(entry.get('component', ''))
                if entry is not None
                else (
                    ranked_cause.component
                    if ranked_cause is not None
                    else req.hypothesis_id
                )
            )
            statement = (
                str(entry.get('statement', ''))
                if entry is not None
                else (
                    ranked_cause.statement if ranked_cause is not None else ''
                )
            )
            request_views.append(
                DataRequestView(
                    hypothesis_id=req.hypothesis_id,
                    component=component,
                    hypothesis_statement=statement,
                    what=req.what,
                    source=req.source,
                    effect=req.effect,
                )
            )

    charts = extract_cited_charts(
        final, store, db_path=db_path, split_hint=split_hint
    )
    counters = final.counters
    tool_calls = _as_int(counters.get('tool_calls', 0))
    model_turns = _as_int(counters.get('model_turns', 0))
    cost_usd = str(counters.get('cost_usd', '0'))
    elapsed_s = _as_float(counters.get('elapsed_s', 0.0))

    banner = determine_state_banner(
        has_usable_model=True,
        use_fake=True,
        budget_exhausted=final.stop_reason in _BUDGET_STOPS,
        final=final,
        events=events,
    )
    markdown = render.render(final, render.call_tools(events))

    return ReportViewModel(
        run_id=final.run_id,
        continuation=final.continuation,
        outcome=final.outcome,
        outcome_text=_OUTCOME_TEXT[final.outcome],
        stop_reason=final.stop_reason,
        fault_type=final.fault_type,
        banner=banner,
        causes=tuple(causes_out),
        actions=action_views,
        data_requests=tuple(request_views),
        dropped=final.dropped,
        charts=charts,
        estimated_onset=estimated_onset,
        valid_citations=valid_citations,
        total_citations=total_citations,
        tool_calls=tool_calls,
        model_turns=model_turns,
        cost_usd=cost_usd,
        elapsed_s=elapsed_s,
        prompt_hash=final.prompt_hash,
        markdown=markdown,
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except ValueError:
        return 0


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return 0.0


def build_live_view(events: Sequence[Mapping[str, object]]) -> LiveViewModel:
    """Build a live view snapshot from partial or complete trajectory events.

    Args:
        events: Events read so far from `trajectory.jsonl`.

    Returns:
        Live tool calls, ledger entries, counters, and role lanes.
    """
    active_node: str | None = None
    tool_calls_count = 0
    model_turns_count = 0
    cost_usd = '0'
    elapsed_s = 0.0
    by_role_calls: dict[str, list[LiveToolCall]] = {}
    by_role_turns: dict[str, int] = {}
    errors: list[str] = []

    for event in events:
        etype = event.get('type')
        role = str(event.get('role', 'agent'))
        if etype == 'node':
            active_node = str(event.get('node', ''))
        elif etype == 'model_call':
            model_turns_count += 1
            by_role_turns[role] = by_role_turns.get(role, 0) + 1
            budget = event.get('budget')
            if isinstance(budget, Mapping):
                tool_calls_count = _as_int(
                    budget.get('tool_calls', tool_calls_count)
                )
                model_turns_count = max(
                    model_turns_count,
                    _as_int(budget.get('model_turns', model_turns_count)),
                )
                cost_usd = str(budget.get('cost_usd', cost_usd))
                elapsed_s = _as_float(budget.get('elapsed_s', elapsed_s))
        elif etype == 'tool_call':
            tool_calls_count += 1
            args = event.get('args')
            locator = event.get('locator')
            targets = event.get('target_components')
            call_item = LiveToolCall(
                seq=_as_int(event.get('seq', 0)),
                call_id=str(event.get('call_id', '')),
                continuation=_as_int(event.get('continuation', 0)),
                role=role,
                node=str(event.get('node', '')),
                tool=str(event.get('tool', '')),
                args=dict(args) if isinstance(args, Mapping) else {},
                ok=bool(event.get('ok', False)),
                locator=dict(locator) if isinstance(locator, Mapping) else None,
                target_components=tuple(
                    str(item)
                    for item in (targets if isinstance(targets, list) else [])
                ),
            )
            by_role_calls.setdefault(role, []).append(call_item)
        elif etype == 'model_error':
            errors.append(
                f'{event.get("error", "Error")}: {event.get("message", "")}'
            )

    latest_entries = _latest_ledger_entries(events)
    hypotheses = tuple(
        LiveHypothesis(
            id=str(entry.get('id', '')),
            component=str(entry.get('component', '')),
            statement=str(entry.get('statement', '')),
            status=str(entry.get('status', 'open')),
            confidence=str(entry.get('confidence', 'low')),
            verification=str(entry.get('verification', 'unverified')),
            evidence_for=(
                len(ev_for)
                if isinstance(ev_for := entry.get('evidence_for'), list)
                else 0
            ),
            evidence_against=(
                len(ev_against)
                if isinstance(ev_against := entry.get('evidence_against'), list)
                else 0
            ),
            origin=str(entry.get('origin', 'data')),
        )
        for entry in latest_entries.values()
    )

    all_roles = sorted(set(by_role_calls) | set(by_role_turns))
    lanes = {
        role: RoleLane(
            role=role,
            tool_calls=tuple(by_role_calls.get(role, ())),
            model_turns=by_role_turns.get(role, 0),
        )
        for role in all_roles
    }

    return LiveViewModel(
        active_node=active_node,
        tool_calls=tool_calls_count,
        model_turns=model_turns_count,
        cost_usd=cost_usd,
        elapsed_s=elapsed_s,
        lanes=lanes,
        hypotheses=hypotheses,
        errors=tuple(errors),
    )


def build_continuation_diff(
    previous: report.Report,
    current: report.Report,
    events: Sequence[Mapping[str, object]],
) -> ContinuationDiffView:
    """Summarise what changed between two passes of an investigation.

    Args:
        previous: Earlier report (typically continuation `c - 1`).
        current: Newer report (continuation `c`).
        events: Trajectory events containing the `continuation` event.

    Returns:
        Structured comparison of catalog additions, ranks, outcomes, and checks.
    """
    added_files: tuple[str, ...] = ()
    catalog_changes: tuple[str, ...] = ()
    for event in events:
        if (
            event.get('type') == 'continuation'
            and _as_int(event.get('continuation', -1)) == current.continuation
        ):
            raw_added = event.get('added')
            raw_changes = event.get('changes')
            added_files = tuple(
                str(item)
                for item in (raw_added if isinstance(raw_added, list) else ())
            )
            catalog_changes = tuple(
                str(item)
                for item in (
                    raw_changes if isinstance(raw_changes, list) else ()
                )
            )

    prev_verify = {
        c.component.casefold(): c.verification for c in previous.causes
    }
    curr_verify = {
        c.component.casefold(): c.verification for c in current.causes
    }
    verify_changes: list[str] = []
    for cause in current.causes:
        key = cause.component.casefold()
        old = prev_verify.get(key)
        now = curr_verify[key]
        if old is None:
            verify_changes.append(f'{cause.component}: new ({now})')
        elif old != now:
            verify_changes.append(f'{cause.component}: {old} -> {now}')

    prev_calls = {
        ref.call_id for cause in previous.causes for ref in cause.evidence
    }
    curr_calls = [
        ref.call_id for cause in current.causes for ref in cause.evidence
    ]
    new_calls = tuple(
        dict.fromkeys(cid for cid in curr_calls if cid not in prev_calls)
    )

    return ContinuationDiffView(
        previous_continuation=previous.continuation,
        current_continuation=current.continuation,
        added_files=added_files,
        catalog_changes=catalog_changes,
        previous_outcome=previous.outcome,
        current_outcome=current.outcome,
        previous_ranks=previous.ranks,
        current_ranks=current.ranks,
        verification_changes=tuple(verify_changes),
        new_evidence_calls=new_calls,
    )
