"""Streamlit web UI for the Incidents Agent (Wave 4 single mode & replay)."""

import dataclasses
import json
import pathlib
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Literal

import streamlit as st
from streamlit import delta_generator
from streamlit.runtime import uploaded_file_manager

from app import runtime
from app import view
from incidents_agent.agent import report
from incidents_agent.agent import state
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.llm import config as llm_config

_FILE_KINDS: tuple[ingest_schema.FileKind, ...] = (
    'tabular',
    'log',
    'document',
)
_CHUNK_STRATEGIES: tuple[ingest_schema.ChunkStrategy, ...] = (
    'none',
    'headings',
    'paragraphs',
    'hunks',
    'sections',
    'whole',
)
_TIME_UNITS: tuple[ingest_schema.TimeUnit, ...] = (
    'unix_s',
    'unix_ms',
    'unix_us',
    'unix_ns',
    'iso8601',
    'unknown',
)
_FEEDBACK_OPTIONS: tuple[
    Literal['confirmed', 'rejected', 'unverified'], ...
] = (
    'confirmed',
    'rejected',
    'unverified',
)


def _init_session_state() -> None:
    defaults: dict[str, object] = {
        'session_dir': None,
        'staged_folder': None,
        'staged_signature': None,
        'base_task': None,
        'classification_overrides': {},
        'time_overrides': {},
        'ingest_preview': None,
        'privacy_acknowledged': False,
        'active_run_dir': None,
        'active_run_events': (),
        'active_run_report': None,
        'active_run_error': None,
        'active_fault_start_s': None,
        'active_use_fake': False,
        'continuation_stage': None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


@dataclasses.dataclass(frozen=True)
class _ContinuationStage:
    """Files staged for one run's continuation, kept across Streamlit reruns.

    Attributes:
        owner: Report key prefix the stage belongs to.
        signature: Name and size of each uploaded file.
        folder: Temporary folder holding the copies, or None after an error.
        paths: Staged file paths.
        preview: Catalog preview, when staging succeeded.
        error: Why staging failed, when it did.
    """

    owner: str
    signature: tuple[tuple[str, int], ...]
    folder: pathlib.Path | None
    paths: tuple[pathlib.Path, ...]
    preview: runtime.ContinuationPreview | None
    error: str | None


def _discard_continuation_stage() -> None:
    stage = st.session_state.get('continuation_stage')
    if isinstance(stage, _ContinuationStage):
        runtime.cleanup_session_dir(stage.folder)
    st.session_state['continuation_stage'] = None


def _stage_continuation(
    owner: str,
    run_dir: pathlib.Path,
    uploads: Sequence[uploaded_file_manager.UploadedFile],
) -> _ContinuationStage | None:
    """Stage uploads once per distinct selection, not on every rerun.

    Streamlit reruns the script on every interaction. Copying the files and
    re-ingesting the combined inputs each time would pile up temporary copies
    of private data, so the stage is cached by selection and removed when the
    selection changes, is cleared, or is used.

    Args:
        owner: Report key prefix the stage belongs to.
        run_dir: Run being continued.
        uploads: Files chosen in the uploader.

    Returns:
        The stage, or None when nothing is selected.
    """
    if not uploads:
        _discard_continuation_stage()
        return None
    signature = tuple((item.name, len(item.getvalue())) for item in uploads)
    current = st.session_state.get('continuation_stage')
    if (
        isinstance(current, _ContinuationStage)
        and current.owner == owner
        and current.signature == signature
    ):
        return current
    _discard_continuation_stage()
    folder = pathlib.Path(tempfile.mkdtemp(prefix='incidents-add-'))
    paths: list[pathlib.Path] = []
    for item in uploads:
        target = folder / pathlib.Path(item.name).name
        target.write_bytes(item.getvalue())
        paths.append(target)
    try:
        stage = _ContinuationStage(
            owner,
            signature,
            folder,
            tuple(paths),
            runtime.preview_continuation(run_dir, paths),
            None,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        runtime.cleanup_session_dir(folder)
        stage = _ContinuationStage(owner, signature, None, (), None, str(exc))
    st.session_state['continuation_stage'] = stage
    return stage


def _paid_action_blocker(
    status: runtime.RuntimeStatus, use_fake: bool
) -> str | None:
    """Explain why a paid model call cannot start now, or None when it can."""
    if use_fake:
        return None
    if not status.usable_aliases:
        return 'No usable model is configured.'
    if status.remaining_usd <= 0 or status.halted:
        return 'The budget is exhausted or halted.'
    return None


def _paid_action_allowed(blocker: str | None, use_fake: bool, key: str) -> bool:
    """Apply the same model, budget and privacy checks as a first run."""
    if blocker is not None:
        st.warning(blocker)
        return False
    return _render_privacy_gate(use_fake, key=key)


def _reset_staged_session() -> None:
    old_dir = st.session_state.get('session_dir')
    if isinstance(old_dir, pathlib.Path):
        runtime.cleanup_session_dir(old_dir)
    st.session_state['session_dir'] = None
    st.session_state['staged_folder'] = None
    st.session_state['staged_signature'] = None
    st.session_state['base_task'] = None
    st.session_state['classification_overrides'] = {}
    st.session_state['time_overrides'] = {}
    st.session_state['ingest_preview'] = None


def _render_banner(banner: view.StateBanner) -> None:
    text = f'**{banner.title}** — {banner.detail}'
    if banner.level == 'error':
        st.error(text)
    elif banner.level == 'warning':
        st.warning(text)
    elif banner.level == 'success':
        st.success(text)
    else:
        st.info(text)


def _parse_fault_start(raw_text: str) -> tuple[float | None, str | None]:
    stripped = raw_text.strip()
    if not stripped:
        return None, None
    try:
        return float(stripped), None
    except ValueError:
        return None, 'Incident start time must be a number in Unix seconds.'


def _render_sidebar(
    status: runtime.RuntimeStatus,
) -> tuple[bool, str, float | None, str | None, str]:
    st.sidebar.header('Configuration')
    st.sidebar.metric(
        'Spend guard remaining',
        f'${status.remaining_usd:.4f}',
        delta=f'Limit ${status.limit_usd:.2f} | Spent ${status.spent_usd:.4f}',
    )
    if status.halted:
        st.sidebar.error('Spend guard halted pending reconciliation.')
    elif status.held_usd > 0:
        st.sidebar.warning(f'Held reservations: ${status.held_usd:.4f}')

    use_fake_choice = st.sidebar.radio(
        'Model mode',
        options=(
            ['Provider model', 'Free demo (--fake)']
            if status.usable_aliases
            else ['Free demo (--fake)', 'Provider model']
        ),
        help=(
            'Free demo uses the rule-based fake client at $0.00 to verify '
            'wiring without calling a provider.'
        ),
    )
    use_fake = use_fake_choice == 'Free demo (--fake)'

    selected_alias = status.default_alias
    if not use_fake:
        if status.usable_aliases:
            default_idx = (
                list(status.usable_aliases).index(status.default_alias)
                if status.default_alias in status.usable_aliases
                else 0
            )
            selected_alias = st.sidebar.selectbox(
                'Model',
                options=list(status.usable_aliases),
                index=default_idx,
            )
        else:
            st.sidebar.warning('No usable provider models found.')
        with st.sidebar.expander('Configured model status'):
            for item in status.models:
                state_txt = 'usable' if item.usable else '; '.join(item.issues)
                st.write(f'- `{item.alias}` ({item.model_id}): {state_txt}')

    st.sidebar.radio(
        'Agent mode',
        options=['single', 'multi (Wave 5 — requires Phase 07)'],
        index=0,
        disabled=True,
        help='Multi-agent role lanes are enabled in Wave 5.',
    )

    st.sidebar.divider()
    st.sidebar.subheader('Optional incident context')
    raw_time = st.sidebar.text_input(
        'Incident start time (Unix seconds)',
        value='',
        placeholder='e.g. 1700001200 (leave blank to auto-detect)',
    )
    fault_start_s, time_err = _parse_fault_start(raw_time)
    if time_err:
        st.sidebar.error(time_err)
    question = st.sidebar.text_area(
        'Incident description / symptoms',
        value='',
        placeholder='Optional notes about symptoms or affected services...',
    )
    st.sidebar.caption(
        f'Per-case cost cap: ${status.per_case_cost_cap_usd:.2f}'
    )
    return use_fake, selected_alias, fault_start_s, time_err, question


def _render_privacy_gate(use_fake: bool, key: str = 'privacy_checkbox') -> bool:
    if use_fake:
        return True
    st.info(
        '**Privacy notice:** Uploaded content is sent to the chosen model '
        'provider. Secrets are masked by pattern only (`tools/safety.py`), '
        'not guaranteed. Excerpts of the files remain in the saved trajectory '
        '(and, if memory is enabled, in incident memory) after the temporary '
        'upload folder is deleted.'
    )
    acknowledged = st.checkbox(
        'I understand and want to send this investigation to the model '
        'provider.',
        value=bool(st.session_state.get('privacy_acknowledged', False)),
        key=key,
    )
    st.session_state['privacy_acknowledged'] = acknowledged
    return bool(acknowledged)


def _refresh_preview(staged_folder: pathlib.Path) -> runtime.IngestPreview:
    cls_overrides: dict[
        str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
    ] = dict(st.session_state.get('classification_overrides', {}))
    time_overrides: dict[str, runtime.TimeOverride] = dict(
        st.session_state.get('time_overrides', {})
    )
    preview = runtime.preview_ingest(
        staged_folder,
        classification_overrides=cls_overrides or None,
        time_overrides=time_overrides or None,
    )
    st.session_state['ingest_preview'] = preview
    return preview


def _render_ingest_section(
    staged_folder: pathlib.Path,
) -> runtime.IngestPreview:
    preview = st.session_state.get('ingest_preview')
    if not isinstance(preview, runtime.IngestPreview):
        preview = _refresh_preview(staged_folder)
    catalog = preview.catalog

    st.subheader('Ingest summary')
    col1, col2, col3 = st.columns(3)
    col1.metric('Tables loaded', len(catalog.tables))
    col2.metric('Documents chunked', len(catalog.documents))
    col3.metric('Skipped files', len(catalog.skipped_files))

    if catalog.skipped_files:
        skip_lines = '\n'.join(
            f'- `{skip.relative_path}`: {skip.reason}'
            for skip in catalog.skipped_files
        )
        st.warning(
            f'{len(catalog.skipped_files)} file(s) were skipped during '
            f'ingest:\n{skip_lines}'
        )

    if catalog.tables:
        st.markdown('**Tables**')
        table_rows = [
            {
                'Table': tbl.name,
                'Source': tbl.source_file,
                'Kind': tbl.kind,
                'Rows': tbl.row_count,
                'Columns': len(tbl.columns),
                'Time range (Unix s)': (
                    f'{tbl.time_info.min_time_s:.1f} .. '
                    f'{tbl.time_info.max_time_s:.1f}'
                    if tbl.time_info.time_usable
                    and tbl.time_info.min_time_s is not None
                    and tbl.time_info.max_time_s is not None
                    else 'not usable'
                ),
            }
            for tbl in catalog.tables
        ]
        st.dataframe(table_rows, width='stretch', hide_index=True)

        st.markdown('**Time mapping per table**')
        time_rows = [
            {
                'Table': item.table_name,
                'Column': item.column or 'none',
                'Unit': item.unit,
                'Confidence': round(item.confidence, 3),
                'Usable': item.time_usable,
                'Status': (
                    'Confirmed'
                    if item.confirmed
                    else 'Needs confirmation or correction'
                ),
                'Reason': item.reason,
            }
            for item in preview.table_time_statuses
        ]
        st.dataframe(time_rows, width='stretch', hide_index=True)

        flagged = [
            item for item in preview.table_time_statuses if item.needs_attention
        ]
        if flagged and not preview.ready_for_time_analysis:
            st.warning(
                'One or more tables have a missing or low-confidence time '
                'mapping. Confirm or correct them before running.'
            )

        with st.expander(
            'Confirm or correct table time mappings',
            expanded=bool(flagged and not preview.ready_for_time_analysis),
        ):
            time_overrides: dict[str, runtime.TimeOverride] = dict(
                st.session_state.get('time_overrides', {})
            )
            for item in preview.table_time_statuses:
                st.markdown(
                    f'`{item.table_name}` ({item.source_file}) — '
                    f'confidence {item.confidence:.2f}'
                )
                c_col, c_unit, c_btn, c_ack = st.columns([2, 2, 1, 2])
                col_opts = ['(none)', *item.available_columns]
                col_idx = (
                    col_opts.index(item.column)
                    if item.column in col_opts
                    else 0
                )
                chosen_col = c_col.selectbox(
                    'Time column',
                    options=col_opts,
                    index=col_idx,
                    key=f'time_col_{item.table_name}',
                )
                unit_idx = (
                    _TIME_UNITS.index(item.unit)
                    if item.unit in _TIME_UNITS
                    else 0
                )
                chosen_unit = c_unit.selectbox(
                    'Unit',
                    options=list(_TIME_UNITS),
                    index=unit_idx,
                    key=f'time_unit_{item.table_name}',
                )
                if c_btn.button('Apply', key=f'apply_time_{item.table_name}'):
                    if chosen_col == '(none)' or chosen_unit == 'unknown':
                        st.error('Pick a column and concrete timestamp unit.')
                    else:
                        time_overrides[item.table_name] = runtime.TimeOverride(
                            column=chosen_col,
                            unit=chosen_unit,
                            confirmed=True,
                        )
                        st.session_state['time_overrides'] = time_overrides
                        try:
                            preview = _refresh_preview(staged_folder)
                            st.rerun()
                        except (OSError, ValueError) as exc:
                            st.error(str(exc))
                if item.needs_attention:
                    is_conf = c_ack.checkbox(
                        'Proceed without usable time',
                        value=item.confirmed,
                        key=f'confirm_time_{item.table_name}',
                    )
                    if is_conf != item.confirmed:
                        time_overrides[item.table_name] = runtime.TimeOverride(
                            column=None,
                            unit='unknown',
                            confirmed=is_conf,
                        )
                        st.session_state['time_overrides'] = time_overrides
                        preview = _refresh_preview(staged_folder)
                        st.rerun()

    if catalog.documents:
        st.markdown('**Documents**')
        for doc in catalog.documents:
            with st.expander(
                f'{doc.title} (`{doc.source_file}`, {doc.doc_type}, '
                f'strategy={doc.chunk_strategy}, {doc.chunk_count} chunks)'
            ):
                for outline_item in doc.outline:
                    st.write(
                        f'- `{outline_item.chunk_id}`: '
                        f'{outline_item.heading_path}'
                    )

    if catalog.classifications:
        with st.expander('File classifications & overrides'):
            cls_rows = [
                {
                    'File': cls_entry.relative_path,
                    'Kind': cls_entry.kind,
                    'Strategy': cls_entry.chunk_strategy,
                    'Confidence': round(cls_entry.confidence, 2),
                    'Reason': cls_entry.reason,
                }
                for cls_entry in catalog.classifications
            ]
            st.dataframe(cls_rows, width='stretch', hide_index=True)
            pending: dict[
                str,
                tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy],
            ] = dict(st.session_state.get('classification_overrides', {}))
            for cls_entry in catalog.classifications:
                fc1, fc2, fc3 = st.columns([3, 2, 2])
                fc1.write(f'`{cls_entry.relative_path}`')
                new_kind = fc2.selectbox(
                    'Kind',
                    options=list(_FILE_KINDS),
                    index=_FILE_KINDS.index(cls_entry.kind),
                    key=f'cls_kind_{cls_entry.relative_path}',
                )
                strat_options: list[ingest_schema.ChunkStrategy] = (
                    ['none']
                    if new_kind in ('tabular', 'log')
                    else [s for s in _CHUNK_STRATEGIES if s != 'none']
                )
                curr_strat: ingest_schema.ChunkStrategy = (
                    cls_entry.chunk_strategy
                    if cls_entry.chunk_strategy in strat_options
                    else strat_options[0]
                )
                new_strat = fc3.selectbox(
                    'Chunking',
                    options=strat_options,
                    index=strat_options.index(curr_strat),
                    key=f'cls_strat_{cls_entry.relative_path}',
                )
                if (new_kind, new_strat) != (
                    cls_entry.kind,
                    cls_entry.chunk_strategy,
                ):
                    pending[cls_entry.relative_path] = (new_kind, new_strat)
            if st.button('Apply classification overrides & re-ingest'):
                st.session_state['classification_overrides'] = pending
                _refresh_preview(staged_folder)
                st.rerun()

    return preview


def _render_live_snapshot(
    live_box: delta_generator.DeltaGenerator,
    events: list[dict[str, object]],
) -> None:
    vm = view.build_live_view(events)
    with live_box.container():
        m1, m2, m3, m4 = st.columns(4)
        m1.metric('Active node', vm.active_node or 'starting')
        m2.metric('Tool calls', vm.tool_calls)
        m3.metric('Model turns', vm.model_turns)
        m4.metric('Cost (USD)', f'${vm.cost_usd}')

        for role, lane in vm.lanes.items():
            st.markdown(f'**Role lane: `{role}`** ({lane.model_turns} turns)')
            if lane.tool_calls:
                rows = [
                    {
                        'Seq': c.seq,
                        'Call ID': c.call_id,
                        'Node': c.node,
                        'Tool': c.tool,
                        'OK': c.ok,
                        'Args': json.dumps(c.args, ensure_ascii=False)[:100],
                    }
                    for c in lane.tool_calls
                ]
                st.dataframe(rows, width='stretch', hide_index=True)

        if vm.hypotheses:
            st.markdown('**Hypothesis ledger**')
            led_rows = [
                {
                    'ID': h.id,
                    'Component': h.component,
                    'Status': h.status,
                    'Verification': h.verification,
                    'Confidence': h.confidence,
                    'For / Against': f'{h.evidence_for} / {h.evidence_against}',
                    'Statement': h.statement,
                }
                for h in vm.hypotheses
            ]
            st.dataframe(led_rows, width='stretch', hide_index=True)


def _execute_investigation(
    task: state.Task,
    *,
    use_fake: bool,
    model_alias: str,
    classification_overrides: Mapping[
        str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
    ],
    cleanup_session: pathlib.Path | None,
) -> None:
    st.subheader('Live investigation')
    live_placeholder = st.empty()
    collected: list[dict[str, object]] = []

    def _on_event(event: dict[str, object]) -> None:
        collected.append(event)
        _render_live_snapshot(live_placeholder, collected)

    try:
        result = runtime.run_app_investigation(
            task,
            use_fake=use_fake,
            model_alias=model_alias if not use_fake else None,
            classification_overrides=classification_overrides or None,
            on_event=_on_event,
            cleanup_session_folder=cleanup_session,
        )
    except Exception as exc:  # noqa: BLE001 - surface run errors clearly in UI
        st.session_state['active_run_error'] = f'{type(exc).__name__}: {exc}'
        st.session_state['active_run_report'] = None
        st.session_state['active_run_dir'] = None
        st.session_state['active_run_events'] = tuple(collected)
        if cleanup_session is not None:
            _reset_staged_session()
        return

    st.session_state['active_run_dir'] = result.run_dir
    st.session_state['active_run_report'] = result.report
    st.session_state['active_run_events'] = result.events
    st.session_state['active_run_error'] = None
    st.session_state['active_fault_start_s'] = task.fault_start_s
    st.session_state['active_use_fake'] = use_fake
    if cleanup_session is not None:
        st.session_state['session_dir'] = None
        st.session_state['staged_folder'] = None
        st.session_state['ingest_preview'] = None


def _render_charts(charts: tuple[view.MetricChart, ...]) -> None:
    if not charts:
        return
    st.subheader('Cited metric charts')
    for chart in charts:
        caption = f'`{chart.table}` · `{chart.column}` (from `{chart.call_id}`)'
        if chart.split_time_s is not None:
            caption += f' — split/onset: `{chart.split_time_s}`'
        st.markdown(caption)
        if chart.kind == 'time_series':
            ts_rows = [
                {'timestamp_s': pt.timestamp_s, 'value': pt.value}
                for pt in chart.points
            ]
            st.line_chart(ts_rows, x='timestamp_s', y='value')
        else:
            cmp_rows = [
                {'window': pt.label, 'value': pt.value} for pt in chart.points
            ]
            st.bar_chart(cmp_rows, x='window', y='value')


def _render_report_section(
    run_dir: pathlib.Path | None,
    loaded_report: view.ReportViewModel,
    events: tuple[dict[str, object], ...],
    *,
    raw_report: report.Report,
    db_path: pathlib.Path | None,
    use_fake: bool,
    model_alias: str,
    key_prefix: str,
    runs_root: pathlib.Path,
    paid_blocker: str | None,
) -> None:
    _render_banner(loaded_report.banner)
    st.caption(
        f'Run `{loaded_report.run_id}` (pass `c{loaded_report.continuation}`) '
        f'· Stop reason `{loaded_report.stop_reason}` '
        f'· Fault type: {loaded_report.fault_type or "not stated"}'
    )
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric('Outcome', loaded_report.outcome)
    m2.metric('Tool calls', loaded_report.tool_calls)
    m3.metric('Model turns', loaded_report.model_turns)
    m4.metric('Cost', f'${loaded_report.cost_usd}')
    m5.metric(
        'Citations valid',
        f'{loaded_report.valid_citations} / {loaded_report.total_citations}',
    )

    if loaded_report.estimated_onset is not None:
        onset = loaded_report.estimated_onset
        with st.container(border=True):
            st.markdown(
                f'**Estimated incident onset (`{onset.call_id}`):** '
                f'`{onset.estimated_onset_time_s}` (confidence: '
                f'{onset.confidence}; top signal: {onset.top_signal})'
            )
            inputs_c0 = (
                run_dir / 'inputs' / 'c0' if run_dir is not None else None
            )
            if inputs_c0 is not None and inputs_c0.is_dir():
                onset_allowed = _paid_action_allowed(
                    paid_blocker, use_fake, f'{key_prefix}_onset_privacy'
                )
                c_in, c_btn = st.columns([2, 1])
                new_onset = c_in.text_input(
                    'Correct onset time (Unix seconds)',
                    value=str(onset.estimated_onset_time_s),
                    key=f'{key_prefix}_onset_input',
                )
                if c_btn.button(
                    'Re-run with corrected onset',
                    key=f'{key_prefix}_onset_rerun',
                    disabled=not onset_allowed,
                ):
                    parsed, err = _parse_fault_start(new_onset)
                    if err or parsed is None:
                        st.error(err or 'Enter a valid numeric onset time.')
                    else:
                        sess = runtime.create_session_dir()
                        staged_dir, case_task = runtime.stage_case_folder(
                            sess, inputs_c0, suite='field'
                        )
                        rerun_task = dataclasses.replace(
                            case_task,
                            folder=staged_dir,
                            fault_start_s=parsed,
                        )
                        _execute_investigation(
                            rerun_task,
                            use_fake=use_fake,
                            model_alias=model_alias,
                            classification_overrides={},
                            cleanup_session=sess,
                        )
                        st.rerun()

    st.subheader('Ranked causes')
    if not loaded_report.causes:
        st.write('No cause is ranked.')
    for cause in loaded_report.causes:
        badge = '' if cause.cited else ' (uncited)'
        header = (
            f'{cause.rank}. {cause.component}{badge} — '
            f'confidence: {cause.confidence} | verification: '
            f'{cause.verification}'
        )
        with st.container(border=True):
            st.markdown(f'### {header}')
            st.write(cause.statement)
            st.markdown('**Why this confidence:**')
            for reason in cause.confidence_reasons:
                st.write(f'- {reason}')
            if cause.evidence:
                st.markdown('**Evidence:**')
                for idx, ev in enumerate(cause.evidence, 1):
                    valid_label = 'valid' if ev.valid else 'invalid'
                    title = (
                        f'{idx}. [{ev.stance}] `{ev.call_id}` (`{ev.tool}`) '
                        f'at `{ev.locator}` — {valid_label}'
                    )
                    with st.expander(title):
                        st.write(f'Quoted value: "{ev.quote}"')
                        st.json(dict(ev.args))
                        if ev.document_chunk is not None:
                            chunk = ev.document_chunk
                            st.markdown(
                                f'**Document:** `{chunk.doc_id}` · '
                                f'**Chunk:** `{chunk.chunk_id}` · '
                                f'**Heading:** `{chunk.heading_path}`'
                            )
                            if chunk.text:
                                st.code(chunk.text, language='text')

    _render_charts(loaded_report.charts)

    st.subheader('Recommended actions')
    if loaded_report.actions:
        for idx, act in enumerate(loaded_report.actions, 1):
            st.markdown(
                f'{idx}. **{act.risk_label}** for `{act.hypothesis_id}`: '
                f'{act.action} — *How to verify:* {act.how_to_verify}'
            )
    else:
        st.write('None.')

    if loaded_report.data_requests:
        st.subheader('Data that would settle it')
        for req in loaded_report.data_requests:
            st.markdown(
                f'- **{req.hypothesis_id} ({req.component})**: {req.what} '
                f'(from {req.source}). *Settles:* {req.effect}'
            )

    if loaded_report.dropped:
        with st.expander('Dropped by validation'):
            for item in loaded_report.dropped:
                st.write(f'- {item}')

    st.download_button(
        'Download report (.md)',
        data=loaded_report.markdown,
        file_name=f'{loaded_report.run_id}-c{loaded_report.continuation}.md',
        mime='text/markdown',
        key=f'{key_prefix}_download_md',
    )

    if run_dir is None or not run_dir.is_dir():
        return

    traj_path = run_dir / 'trajectory.jsonl'
    if loaded_report.continuation > 0 and traj_path.is_file():
        try:
            prev = runtime.load_past_run(
                traj_path, loaded_report.continuation - 1
            )
            diff = view.build_continuation_diff(
                prev.report,
                raw_report,
                events,
            )
            with st.expander(
                f'What changed from pass c{diff.previous_continuation} '
                f'to c{diff.current_continuation}',
                expanded=True,
            ):
                st.write(
                    f'- Outcome: `{diff.previous_outcome}` -> '
                    f'`{diff.current_outcome}`'
                )
                st.write(
                    f'- Ranks: `{list(diff.previous_ranks)}` -> '
                    f'`{list(diff.current_ranks)}`'
                )
                if diff.added_files:
                    st.write(f'- Added files: {", ".join(diff.added_files)}')
                for line in diff.catalog_changes:
                    st.write(f'  {line}')
                for change in diff.verification_changes:
                    st.write(f'- Verification: {change}')
                if diff.new_evidence_calls:
                    st.write(
                        '- New evidence calls: '
                        f'{", ".join(diff.new_evidence_calls)}'
                    )
        except ValueError:
            pass

    if (run_dir / 'checkpoint.json').is_file():
        with st.expander('Add files and continue'):
            extra_uploads = st.file_uploader(
                'Additional files for continuation',
                accept_multiple_files=True,
                key=f'{key_prefix}_continue_files',
            )
            stage = _stage_continuation(
                key_prefix, run_dir, extra_uploads or []
            )
            if stage is not None and stage.error is not None:
                st.error(stage.error)
            elif stage is not None and stage.preview is not None:
                st.write('**Catalog additions preview:**')
                for line in stage.preview.changes:
                    st.write(line)
                st.write(
                    'Estimated continuation cost cap: '
                    f'${stage.preview.estimated_cost_usd:.2f}'
                )
                cont_allowed = _paid_action_allowed(
                    paid_blocker, use_fake, f'{key_prefix}_cont_privacy'
                )
                if st.button(
                    'Continue run with added files',
                    key=f'{key_prefix}_continue_btn',
                    disabled=not cont_allowed,
                ):
                    cont_live = st.empty()
                    cont_events = list(events)

                    def _on_cont_event(ev: dict[str, object]) -> None:
                        cont_events.append(ev)
                        _render_live_snapshot(cont_live, cont_events)

                    # The run removes the staged folder when it ends.
                    st.session_state['continuation_stage'] = None
                    try:
                        cont_res = runtime.continue_app_run(
                            run_dir,
                            list(stage.paths),
                            use_fake=use_fake,
                            model_alias=model_alias if not use_fake else None,
                            on_event=_on_cont_event,
                            cleanup_added_dir=stage.folder,
                        )
                    except (FileNotFoundError, ValueError, OSError) as exc:
                        st.error(str(exc))
                    else:
                        st.session_state['active_run_report'] = cont_res.report
                        st.session_state['active_run_events'] = cont_res.events
                        st.rerun()

    with st.expander('Feedback (saved locally for Phase 09 memory)'):
        existing_fb = runtime.load_feedback(run_dir)
        default_idx = (
            _FEEDBACK_OPTIONS.index(existing_fb.outcome)
            if existing_fb is not None
            else 0
        )
        fb_outcome = st.radio(
            'Was the diagnosis correct?',
            options=list(_FEEDBACK_OPTIONS),
            index=default_idx,
            key=f'{key_prefix}_fb_outcome',
        )
        fb_root = st.text_input(
            'Actual root cause',
            value=existing_fb.actual_root_cause if existing_fb else '',
            key=f'{key_prefix}_fb_root',
        )
        fb_fix = st.text_input(
            'What fixed it?',
            value=existing_fb.what_fixed_it if existing_fb else '',
            key=f'{key_prefix}_fb_fix',
        )
        fb_notes = st.text_area(
            'Notes',
            value=existing_fb.notes if existing_fb else '',
            key=f'{key_prefix}_fb_notes',
        )
        if st.button('Save feedback', key=f'{key_prefix}_fb_save'):
            runtime.save_feedback(
                run_dir,
                runtime.FeedbackRecord(
                    run_id=loaded_report.run_id,
                    continuation=loaded_report.continuation,
                    outcome=fb_outcome,
                    actual_root_cause=fb_root,
                    what_fixed_it=fb_fix,
                    notes=fb_notes,
                ),
            )
            st.success('Feedback saved locally.')

    with st.expander('Delete this run'):
        confirm_del = st.checkbox(
            'Confirm permanent removal of trajectory and local records',
            key=f'{key_prefix}_del_confirm',
        )
        if st.button(
            'Delete run',
            disabled=not confirm_del,
            key=f'{key_prefix}_del_btn',
        ):
            _discard_continuation_stage()
            runtime.delete_run(run_dir, runs_root=runs_root)
            if st.session_state.get('active_run_dir') == run_dir:
                st.session_state['active_run_dir'] = None
                st.session_state['active_run_report'] = None
                st.session_state['active_run_events'] = ()
            st.success('Run deleted.')
            st.rerun()


def _render_investigate_tab(
    status: runtime.RuntimeStatus,
    use_fake: bool,
    selected_alias: str,
    fault_start_s: float | None,
    time_err: str | None,
    question: str,
) -> None:
    budget_exhausted = status.remaining_usd <= 0 or status.halted
    has_usable = bool(status.usable_aliases)
    pre_banner = view.determine_state_banner(
        has_usable_model=has_usable,
        use_fake=use_fake,
        budget_exhausted=budget_exhausted,
        run_error=st.session_state.get('active_run_error')
        if isinstance(st.session_state.get('active_run_error'), str)
        else None,
    )
    if pre_banner.kind in ('no_usable_model', 'budget_exhausted', 'run_error'):
        _render_banner(pre_banner)

    privacy_ok = _render_privacy_gate(use_fake)

    source_mode = st.radio(
        'Incident input source',
        options=['Upload files', 'Pick a benchmark or field case'],
        horizontal=True,
    )

    if source_mode == 'Upload files':
        uploaded = st.file_uploader(
            'Drop incident files (metrics CSV/Parquet, logs, docs, diffs)',
            accept_multiple_files=True,
        )
        if uploaded:
            sig = tuple((f.name, len(f.getvalue())) for f in uploaded)
            if st.session_state.get('staged_signature') != ('upload', sig):
                _reset_staged_session()
                sess_dir = runtime.create_session_dir()
                staged = runtime.stage_uploaded_files(
                    sess_dir,
                    [(f.name, f.getvalue()) for f in uploaded],
                )
                st.session_state['session_dir'] = sess_dir
                st.session_state['staged_folder'] = staged
                st.session_state['staged_signature'] = ('upload', sig)
                st.session_state['base_task'] = state.Task(folder=staged)
                _refresh_preview(staged)
    else:
        cases = runtime.list_available_cases()
        options: list[tuple[Literal['field', 'rcaeval'], str]] = [
            ('field', cid) for cid in cases.field_cases
        ] + [('rcaeval', cid) for cid in cases.benchmark_tuning]
        if not options:
            st.info('No field or exported tuning benchmark cases found.')
        else:
            labels = [f'{suite}: {cid}' for suite, cid in options]
            chosen_idx = st.selectbox(
                'Case',
                options=list(range(len(options))),
                format_func=lambda idx: labels[idx],
            )
            suite, case_id = options[chosen_idx]
            if st.session_state.get('staged_signature') != (
                'case',
                suite,
                case_id,
            ):
                _reset_staged_session()
                sess_dir = runtime.create_session_dir()
                folder = runtime.resolve_case_folder(case_id, suite=suite)
                staged, base_task = runtime.stage_case_folder(
                    sess_dir, folder, suite=suite
                )
                st.session_state['session_dir'] = sess_dir
                st.session_state['staged_folder'] = staged
                st.session_state['staged_signature'] = ('case', suite, case_id)
                st.session_state['base_task'] = base_task
                _refresh_preview(staged)

    staged_folder = st.session_state.get('staged_folder')
    preview: runtime.IngestPreview | None = None
    if isinstance(staged_folder, pathlib.Path) and staged_folder.is_dir():
        preview = _render_ingest_section(staged_folder)

    can_run = (
        isinstance(staged_folder, pathlib.Path)
        and preview is not None
        and preview.ready_for_time_analysis
        and privacy_ok
        and time_err is None
        and (use_fake or (has_usable and not budget_exhausted))
    )
    if st.button('Run investigation', type='primary', disabled=not can_run):
        run_task = st.session_state.get('base_task')
        if isinstance(run_task, state.Task):
            effective_task = dataclasses.replace(
                run_task,
                question=question.strip(),
                fault_start_s=(
                    fault_start_s
                    if fault_start_s is not None
                    else run_task.fault_start_s
                ),
            )
            active_sess = st.session_state.get('session_dir')
            cls_ov = st.session_state.get('classification_overrides', {})
            _execute_investigation(
                effective_task,
                use_fake=use_fake,
                model_alias=selected_alias,
                classification_overrides=(
                    cls_ov if isinstance(cls_ov, Mapping) else {}
                ),
                cleanup_session=(
                    active_sess
                    if isinstance(active_sess, pathlib.Path)
                    else None
                ),
            )
            st.rerun()

    active_report = st.session_state.get('active_run_report')
    active_events = st.session_state.get('active_run_events', ())
    active_dir = st.session_state.get('active_run_dir')
    if isinstance(active_report, report.Report) and isinstance(
        active_events, tuple
    ):
        run_dir_path = (
            active_dir if isinstance(active_dir, pathlib.Path) else None
        )
        cont = active_report.continuation
        db_path = (
            run_dir_path / f'db-c{cont}' / 'incident.duckdb'
            if run_dir_path is not None and cont > 0
            else (
                run_dir_path / 'db' / 'incident.duckdb'
                if run_dir_path is not None
                else None
            )
        )
        if db_path is not None and not db_path.is_file():
            db_path = (
                run_dir_path / 'db' / 'incident.duckdb'
                if run_dir_path is not None
                and (run_dir_path / 'db' / 'incident.duckdb').is_file()
                else None
            )
        vm = view.build_report_view(
            active_report,
            active_events,
            db_path=db_path,
            task_fault_start_s=(
                st.session_state.get('active_fault_start_s')
                if isinstance(
                    st.session_state.get('active_fault_start_s'), float
                )
                else None
            ),
        )
        _render_report_section(
            run_dir_path,
            vm,
            active_events,
            raw_report=active_report,
            db_path=db_path,
            use_fake=bool(st.session_state.get('active_use_fake', use_fake)),
            model_alias=selected_alias,
            key_prefix='active',
            runs_root=runtime.DEFAULT_RUNS_DIR,
            paid_blocker=_paid_action_blocker(status, use_fake),
        )


def _render_past_runs_tab(
    status: runtime.RuntimeStatus, use_fake: bool, selected_alias: str
) -> None:
    st.subheader('Past-Runs Viewer (offline replay)')
    summaries = runtime.list_past_runs(runtime.DEFAULT_RUNS_DIR)
    if not summaries:
        st.info('No saved agent trajectories found under `runs/`.')
        return

    labels = [
        f'{item.run_id} — {item.outcome}'
        + (f' ({item.top_cause})' if item.top_cause else '')
        for item in summaries
    ]
    idx = st.selectbox(
        'Saved trajectory',
        options=list(range(len(summaries))),
        format_func=lambda i: labels[i],
    )
    chosen = summaries[idx]
    selected_cont: int | None = None
    if len(chosen.continuations) > 1:
        selected_cont = st.selectbox(
            'Pass (continuation)',
            options=list(chosen.continuations),
            index=len(chosen.continuations) - 1,
            format_func=lambda c: f'c{c}',
        )

    loaded = runtime.load_past_run(chosen.trajectory_path, selected_cont)
    live_vm = view.build_live_view(loaded.events)
    with st.expander('Replayed trajectory steps & ledger', expanded=False):
        for role, lane in live_vm.lanes.items():
            st.markdown(f'**Role: `{role}`** ({lane.model_turns} turns)')
            if lane.tool_calls:
                st.dataframe(
                    [
                        {
                            'Seq': c.seq,
                            'Pass': f'c{c.continuation}',
                            'Call ID': c.call_id,
                            'Node': c.node,
                            'Tool': c.tool,
                            'OK': c.ok,
                            'Args': json.dumps(c.args, ensure_ascii=False)[
                                :100
                            ],
                        }
                        for c in lane.tool_calls
                    ],
                    width='stretch',
                    hide_index=True,
                )
        if live_vm.hypotheses:
            st.dataframe(
                [dataclasses.asdict(h) for h in live_vm.hypotheses],
                width='stretch',
                hide_index=True,
            )

    vm = view.build_report_view(
        loaded.report,
        loaded.events,
        db_path=loaded.db_path,
        task_fault_start_s=loaded.task_fault_start_s,
    )
    _render_report_section(
        loaded.run_dir,
        vm,
        loaded.events,
        raw_report=loaded.report,
        db_path=loaded.db_path,
        use_fake=use_fake,
        model_alias=selected_alias,
        key_prefix=f'replay_{loaded.run_id}_c{loaded.continuation}',
        runs_root=runtime.DEFAULT_RUNS_DIR,
        paid_blocker=_paid_action_blocker(status, use_fake),
    )


def main() -> None:
    """Render the Incidents Agent Streamlit application."""
    st.set_page_config(page_title='Incidents Agent', layout='wide')
    _init_session_state()
    st.title('Incidents Agent')

    try:
        status = runtime.inspect_runtime_status()
    except (llm_config.ConfigError, OSError, ValueError) as exc:
        st.error(f'Configuration error: {exc}')
        return

    use_fake, selected_alias, fault_start_s, time_err, question = (
        _render_sidebar(status)
    )
    tab_investigate, tab_replay = st.tabs(
        ['Investigate Incident', 'Past-Runs Viewer']
    )
    with tab_investigate:
        _render_investigate_tab(
            status,
            use_fake,
            selected_alias,
            fault_start_s,
            time_err,
            question,
        )
    with tab_replay:
        _render_past_runs_tab(status, use_fake, selected_alias)


if __name__ == '__main__':
    main()
