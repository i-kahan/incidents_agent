"""Pure runtime helpers for staging uploads, running, and replaying runs."""

import contextlib
import dataclasses
import datetime
import decimal
import functools
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
import typing
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent import futures
from typing import Literal

import duckdb

from incidents_agent import ingest
from incidents_agent.agent import config as agent_config
from incidents_agent.agent import continuation
from incidents_agent.agent import fake_policy
from incidents_agent.agent import graph
from incidents_agent.agent import ledger as agent_ledger
from incidents_agent.agent import memory_record
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import run_state
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.eval import field
from incidents_agent.eval import selection
from incidents_agent.eval import split
from incidents_agent.ingest import loaders
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.ingest import time_detect
from incidents_agent.llm import client as llm_client
from incidents_agent.llm import config as llm_config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger as spend_ledger
from incidents_agent.llm import litellm_transport
from incidents_agent.llm import pricing
from incidents_agent.memory import store as incident_memory

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _shared_root() -> pathlib.Path:
    """Return the common repository root when running from a git worktree."""
    try:
        common = subprocess.check_output(
            [
                'git',
                'rev-parse',
                '--path-format=absolute',
                '--git-common-dir',
            ],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return pathlib.Path(common).parent
    except (OSError, subprocess.SubprocessError):
        return REPO_ROOT


SHARED_ROOT = _shared_root()

DEFAULT_SPLIT_PATH = REPO_ROOT / 'splits/re2-ob.json'
DEFAULT_TASKS_ROOT = SHARED_ROOT / 'agent_tasks/re2-ob'
DEFAULT_FIELD_ROOT = REPO_ROOT / 'evalsets/field'
DEFAULT_MODELS_PATH = REPO_ROOT / 'config/models.yaml'
DEFAULT_BUDGET_PATH = REPO_ROOT / 'config/budget.yaml'
DEFAULT_AGENT_PATH = REPO_ROOT / agent_config.DEFAULT_PATH
DEFAULT_ENV_PATH = SHARED_ROOT / '.env'
DEFAULT_RUNS_DIR = SHARED_ROOT / 'runs'
DEFAULT_DRY_RUNS_DIR = DEFAULT_RUNS_DIR / 'dry'
DEFAULT_SPEND_PATH = DEFAULT_RUNS_DIR / 'spend.sqlite3'
DEFAULT_MEMORY_PATH = DEFAULT_RUNS_DIR / 'memory.sqlite3'
# Evaluation output (`runs/phase-NN/...`, including `cache/<key>/attempts/`)
# shares `runs/` with app runs. It is never listed, continued or deleted here.
_EVAL_ROOT_PREFIX = 'phase-'
_EVAL_CACHE_PARTS = frozenset({'cache', 'attempts'})

_OVERRIDES_FILE = 'classification_overrides.json'
_IGNORED_CASE_FILES = frozenset({'task.json'})


@dataclasses.dataclass(frozen=True)
class AvailableCases:
    """Selectable benchmark tuning cases and field-set cases.

    Attributes:
        benchmark_tuning: Tuning case ids whose folders exist on disk.
        field_cases: Field-set case ids whose folders exist on disk.
    """

    benchmark_tuning: tuple[str, ...]
    field_cases: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class TimeOverride:
    """User confirmation or correction for a table's timestamp mapping.

    Attributes:
        column: Selected time column name, or None when confirmed without time.
        unit: Selected timestamp unit.
        confirmed: Whether the user confirmed this mapping for analysis.
    """

    column: str | None
    unit: ingest_schema.TimeUnit = 'unix_s'
    confirmed: bool = True


@dataclasses.dataclass(frozen=True)
class TableTimeStatus:
    """Time-mapping diagnostics and confirmation status for one table.

    Attributes:
        table_name: DuckDB table name.
        source_file: Relative path to the source file.
        column: Detected or overridden timestamp column name.
        unit: Inferred or overridden timestamp unit.
        confidence: Confidence score in `[0.0, 1.0]`.
        time_usable: Whether time-window tools may query this table.
        min_time_s: Minimum normalised Unix timestamp in seconds.
        max_time_s: Maximum normalised Unix timestamp in seconds.
        reason: Explanation of the time detection outcome.
        needs_attention: True when time is missing or below threshold.
        confirmed: True when usable with high confidence or user-confirmed.
        available_columns: All column names in the table for override pickers.
    """

    table_name: str
    source_file: str
    column: str | None
    unit: ingest_schema.TimeUnit
    confidence: float
    time_usable: bool
    min_time_s: float | None
    max_time_s: float | None
    reason: str
    needs_attention: bool
    confirmed: bool
    available_columns: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class IngestPreview:
    """Ingest summary and time-mapping readiness before running the agent.

    Attributes:
        catalog: Built ingest catalog.
        table_time_statuses: Per-table time mapping status rows.
        ready_for_time_analysis: True when all flagged tables are resolved.
    """

    catalog: ingest_schema.Catalog
    table_time_statuses: tuple[TableTimeStatus, ...]
    ready_for_time_analysis: bool


@dataclasses.dataclass(frozen=True)
class ModelOption:
    """Availability status for one configured model alias.

    Attributes:
        alias: Model alias from `config/models.yaml`.
        model_id: Provider/model identifier string.
        provider: Provider name.
        usable: True when key and pricing checks pass.
        issues: Reasons the model cannot currently be called.
    """

    alias: str
    model_id: str
    provider: str
    usable: bool
    issues: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class RuntimeStatus:
    """Configured model inventory and global spend guard snapshot.

    Attributes:
        models: All configured models with usability diagnostics.
        usable_aliases: Aliases ready to call.
        default_alias: Default alias configured in `config/models.yaml`.
        limit_usd: Effective global spend limit after safety margin.
        spent_usd: Settled spend so far.
        held_usd: Open reservations held so far.
        remaining_usd: Uncommitted global balance.
        halted: True when an overrun halted admissions.
        per_case_cost_cap_usd: Per-case cost cap from `config/agent.yaml`.
        memory_enabled_default: Default memory flag from `config/agent.yaml`.
        memory_frozen: Whether incident memory is configured read-only.
        memory_records_count: Number of records in the incident memory store.
    """

    models: tuple[ModelOption, ...]
    usable_aliases: tuple[str, ...]
    default_alias: str
    limit_usd: decimal.Decimal
    spent_usd: decimal.Decimal
    held_usd: decimal.Decimal
    remaining_usd: decimal.Decimal
    halted: bool
    per_case_cost_cap_usd: decimal.Decimal
    memory_enabled_default: bool = False
    memory_frozen: bool = False
    memory_records_count: int = 0


@dataclasses.dataclass(frozen=True)
class ContinuationPreview:
    """Summary of added files before executing a run continuation.

    Attributes:
        added_files: File names being added.
        changes: Catalog diff lines from `continuation.what_changed`.
        estimated_cost_usd: Per-continuation cost cap from agent config.
    """

    added_files: tuple[str, ...]
    changes: tuple[str, ...]
    estimated_cost_usd: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class FeedbackRecord:
    """Operator feedback on a finished run, synced with incident memory.

    Attributes:
        run_id: Run identifier.
        continuation: Continuation index feedback applies to.
        outcome: `confirmed`, `rejected`, or `unverified`.
        actual_root_cause: Confirmed or actual root cause when known.
        what_fixed_it: Remediation that resolved the incident.
        notes: Additional operator notes.
        recorded_at: UTC ISO-8601 timestamp.
    """

    run_id: str
    continuation: int
    outcome: Literal['confirmed', 'rejected', 'unverified']
    actual_root_cause: str
    what_fixed_it: str
    notes: str = ''
    recorded_at: str = ''

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'FeedbackRecord':
        """Rebuild a feedback record from JSON."""
        raw_outcome = data.get('outcome', 'unverified')
        outcome: Literal['confirmed', 'rejected', 'unverified'] = (
            raw_outcome
            if raw_outcome in ('confirmed', 'rejected', 'unverified')
            else 'unverified'
        )
        raw_cont = data.get('continuation', 0)
        cont = (
            int(raw_cont)
            if isinstance(raw_cont, int) and not isinstance(raw_cont, bool)
            else 0
        )
        return cls(
            run_id=str(data.get('run_id', '')),
            continuation=cont,
            outcome=outcome,
            actual_root_cause=str(data.get('actual_root_cause', '')),
            what_fixed_it=str(data.get('what_fixed_it', '')),
            notes=str(data.get('notes', '')),
            recorded_at=str(data.get('recorded_at', '')),
        )


@dataclasses.dataclass(frozen=True)
class PastRunSummary:
    """Metadata for one saved agent trajectory under `runs/`.

    Attributes:
        run_id: Run identifier.
        run_dir: Folder holding the trajectory.
        trajectory_path: Path to `trajectory.jsonl`.
        outcome: Latest final report outcome.
        top_cause: Top-ranked component, if any.
        continuations: Available continuation indices recorded in the file.
        updated_at: Modification timestamp of `trajectory.jsonl`.
    """

    run_id: str
    run_dir: pathlib.Path
    trajectory_path: pathlib.Path
    outcome: str
    top_cause: str | None
    continuations: tuple[int, ...]
    updated_at: float


@dataclasses.dataclass(frozen=True)
class LoadedPastRun:
    """Replayed run state loaded from disk without calling any model.

    Attributes:
        run_id: Run identifier.
        run_dir: Run folder.
        trajectory_path: Path to `trajectory.jsonl`.
        continuation: Selected continuation pass index.
        available_continuations: All continuation passes present in the file.
        report: Final report for the selected pass.
        events: Every trajectory event in order.
        valid_citations: Count of citations passing validation.
        total_citations: Total citations across ranked causes.
        db_path: Read-only DuckDB path for this pass if present on disk.
        task_fault_start_s: Known fault start time from manifest if present.
        feedback: Saved local feedback record if present.
    """

    run_id: str
    run_dir: pathlib.Path
    trajectory_path: pathlib.Path
    continuation: int
    available_continuations: tuple[int, ...]
    report: report.Report
    events: tuple[dict[str, object], ...]
    valid_citations: int
    total_citations: int
    db_path: pathlib.Path | None
    task_fault_start_s: float | None
    feedback: FeedbackRecord | None


def create_session_dir(base_dir: pathlib.Path | None = None) -> pathlib.Path:
    """Create a fresh per-session temporary folder for staged uploads.

    Args:
        base_dir: Optional parent directory; defaults to the OS temp directory.

    Returns:
        Path to the created session directory.
    """
    if base_dir is not None:
        base_dir.mkdir(parents=True, exist_ok=True)
    return pathlib.Path(
        tempfile.mkdtemp(prefix='incidents-upload-', dir=base_dir)
    )


def _safe_staged_path(root: pathlib.Path, filename: str) -> pathlib.Path:
    pure = pathlib.PurePosixPath(filename.replace('\\', '/'))
    if (
        not filename.strip()
        or pure.is_absolute()
        or '..' in pure.parts
        or any(part in ('', '.') for part in pure.parts)
    ):
        raise ValueError(f'Unsafe upload path: {filename!r}')
    target = (root / pathlib.Path(*pure.parts)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f'Upload path escapes staging dir: {filename!r}')
    return target


def stage_uploaded_files(
    session_dir: pathlib.Path,
    files: Sequence[tuple[str, bytes]],
) -> pathlib.Path:
    """Write uploaded `(name, bytes)` pairs into `session_dir / 'staged'`.

    Args:
        session_dir: Per-session temporary root created by `create_session_dir`.
        files: Uploaded relative filenames and raw byte payloads.

    Returns:
        Path to the staged folder ready for ingest.

    Raises:
        ValueError: When `files` is empty or any filename is unsafe.
    """
    if not files:
        raise ValueError('Select at least one file to upload')
    staged = session_dir / 'staged'
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True, exist_ok=True)
    for name, content in files:
        dest = _safe_staged_path(staged, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return staged


def stage_case_folder(
    session_dir: pathlib.Path,
    case_folder: pathlib.Path,
    *,
    suite: Literal['rcaeval', 'field'],
) -> tuple[pathlib.Path, state.Task]:
    """Copy a benchmark or field case into the per-session staging folder.

    Args:
        session_dir: Per-session temporary directory.
        case_folder: Source directory containing case inputs.
        suite: `'rcaeval'` (reads `task.json`) or `'field'`.

    Returns:
        `(staged_folder, task)` pointing to the isolated staged copy.

    Raises:
        FileNotFoundError: When `case_folder` is missing.
    """
    if not case_folder.is_dir():
        raise FileNotFoundError(f'Case folder not found: {case_folder}')
    task_json = case_folder / 'task.json'
    if suite == 'rcaeval' and task_json.is_file():
        base_task = state.Task.from_task_json(task_json)
    else:
        base_task = state.Task(folder=case_folder, case_id=case_folder.name)

    staged = session_dir / 'staged'
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(case_folder, staged)
    for ignored in _IGNORED_CASE_FILES:
        candidate = staged / ignored
        if candidate.is_file():
            candidate.unlink()
    return staged, dataclasses.replace(base_task, folder=staged)


def cleanup_session_dir(session_dir: pathlib.Path | None) -> None:
    """Remove a per-session temporary upload directory if it exists."""
    if session_dir is not None and session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def list_available_cases(
    *,
    split_path: pathlib.Path = DEFAULT_SPLIT_PATH,
    tasks_root: pathlib.Path = DEFAULT_TASKS_ROOT,
    field_root: pathlib.Path = DEFAULT_FIELD_ROOT,
) -> AvailableCases:
    """Discover selectable tuning benchmark cases and field-set cases.

    Held-out benchmark cases are excluded via `eval.selection.resolve`.

    Args:
        split_path: Path to locked split JSON.
        tasks_root: Root directory containing exported `cases/<case_id>`.
        field_root: Root directory containing field-set `cases/<case_id>`.

    Returns:
        Sorted tuples of available tuning and field case ids.
    """
    tuning_ids: tuple[str, ...] = ()
    if split_path.is_file():
        partition = split.from_json(
            json.loads(split_path.read_text(encoding='utf-8'))
        )
        selected = selection.resolve('tuning', partition, final=False)
        cases_dir = tasks_root / 'cases'
        tuning_ids = tuple(
            cid for cid in selected if (cases_dir / cid).is_dir()
        )
    field_dir = field_root / 'cases'
    field_ids = tuple(
        cid for cid in field.CASE_IDS if (field_dir / cid).is_dir()
    )
    return AvailableCases(
        benchmark_tuning=tuning_ids,
        field_cases=field_ids,
    )


def resolve_case_folder(
    case_id: str,
    *,
    suite: Literal['rcaeval', 'field'],
    split_path: pathlib.Path = DEFAULT_SPLIT_PATH,
    tasks_root: pathlib.Path = DEFAULT_TASKS_ROOT,
    field_root: pathlib.Path = DEFAULT_FIELD_ROOT,
) -> pathlib.Path:
    """Resolve a case folder while enforcing the held-out gate on benchmark ids.

    Args:
        case_id: Selected case identifier.
        suite: `'rcaeval'` or `'field'`.
        split_path: Path to locked split JSON.
        tasks_root: Exported benchmark tasks root.
        field_root: Field-set root.

    Returns:
        Path to the case folder on disk.

    Raises:
        ValueError: When a held-out or unknown case id is requested.
        FileNotFoundError: When the resolved folder does not exist.
    """
    if suite == 'field':
        if case_id not in field.CASE_IDS:
            raise ValueError(f'Unknown field case ID: {case_id!r}')
        folder = field_root / 'cases' / case_id
    else:
        partition = split.from_json(
            json.loads(split_path.read_text(encoding='utf-8'))
        )
        selection.resolve(case_id, partition, final=False)
        folder = tasks_root / 'cases' / case_id
    if not folder.is_dir():
        raise FileNotFoundError(f'Case folder not found: {folder}')
    return folder


def _write_duckdb_table_to_file(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    file_path: pathlib.Path,
) -> None:
    ext = file_path.suffix.lower()
    table_sql = time_detect.quote_ident(table_name)
    fd, tmp_name = tempfile.mkstemp(dir=file_path.parent, suffix=ext)
    os.close(fd)
    tmp_path = pathlib.Path(tmp_name)
    try:
        tmp_path.unlink(missing_ok=True)
        if ext == '.csv':
            conn.execute(
                f"COPY {table_sql} TO ? (HEADER, DELIMITER ',')",
                [str(tmp_path)],
            )
        elif ext == '.tsv':
            conn.execute(
                f"COPY {table_sql} TO ? (HEADER, DELIMITER '\t')",
                [str(tmp_path)],
            )
        elif ext == '.parquet':
            conn.execute(
                f'COPY {table_sql} TO ? (FORMAT PARQUET)',
                [str(tmp_path)],
            )
        elif ext in ('.jsonl', '.ndjson'):
            conn.execute(
                f'COPY {table_sql} TO ? (FORMAT JSON)',
                [str(tmp_path)],
            )
        elif ext == '.json':
            conn.execute(
                f'COPY {table_sql} TO ? (FORMAT JSON, ARRAY true)',
                [str(tmp_path)],
            )
        else:
            raise ValueError(
                f'Time column override is not supported for {ext!r} files'
            )
        os.replace(tmp_path, file_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def apply_time_override_to_file(
    file_path: pathlib.Path,
    column: str,
    unit: ingest_schema.TimeUnit,
) -> None:
    """Materialise a normalised `unix_time` column on a staged tabular file.

    Args:
        file_path: Path to the staged CSV, TSV, Parquet, or JSON file.
        column: Source column containing timestamps.
        unit: Explicit timestamp unit selected by the user.

    Raises:
        ValueError: When `unit` is `'unknown'`, `column` is missing, or
            normalisation produces only NULLs.
    """
    if unit == 'unknown':
        raise ValueError('Choose a concrete timestamp unit to override')
    conn = duckdb.connect(':memory:')
    try:
        loaders._load_tabular_file(conn, file_path, 'staged_tbl')
        cols = [
            str(row[0])
            for row in conn.execute('DESCRIBE staged_tbl').fetchall()
        ]
        if column not in cols:
            raise ValueError(f'Column {column!r} not found in {file_path.name}')
        expr = time_detect._normalisation_sql(column, unit)
        target_col = 'unix_time'
        if target_col in cols:
            conn.execute(f'ALTER TABLE staged_tbl DROP COLUMN {target_col}')
        conn.execute(f'ALTER TABLE staged_tbl ADD COLUMN {target_col} DOUBLE')
        conn.execute(f'UPDATE staged_tbl SET {target_col} = {expr}')
        cnt_row = conn.execute(
            f'SELECT COUNT({target_col}) FROM staged_tbl'
        ).fetchone()
        valid_count = (
            int(cnt_row[0]) if cnt_row and cnt_row[0] is not None else 0
        )
        if valid_count == 0:
            raise ValueError(
                f'Parsing {column!r} as {unit} produced zero valid timestamps'
            )
        _write_duckdb_table_to_file(conn, 'staged_tbl', file_path)
    finally:
        conn.close()


def preview_ingest(
    folder: pathlib.Path,
    *,
    classification_overrides: Mapping[
        str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
    ]
    | None = None,
    time_overrides: Mapping[str, TimeOverride] | None = None,
    workdir: pathlib.Path | None = None,
) -> IngestPreview:
    """Ingest `folder` for UI preview, applying optional overrides.

    Args:
        folder: Folder containing staged incident files.
        classification_overrides: Optional per-file kind/strategy overrides.
        time_overrides: Optional per-table time-mapping overrides or
            confirmations keyed by table name.
        workdir: Optional scratch directory for preview DuckDB output.

    Returns:
        Catalog summary and per-table time confirmation status.
    """
    temp_work: pathlib.Path | None = None
    if workdir is None:
        temp_work = pathlib.Path(tempfile.mkdtemp(prefix='incidents-preview-'))
        effective_work = temp_work
    else:
        effective_work = workdir

    overrides_map = dict(time_overrides or {})
    ingest_cfg = ingest_schema.IngestConfig(
        classification_overrides=classification_overrides
    )

    try:
        res = ingest.ingest_folder(
            folder, workdir=effective_work, config=ingest_cfg
        )
        try:
            catalog = res.catalog
        finally:
            res.close()

        applied_file_update = False
        for table in catalog.tables:
            override = overrides_map.get(table.name)
            if override is None or override.column is None:
                continue
            if override.unit == 'unknown':
                continue
            if table.kind != 'tabular':
                continue
            already_matches = (
                table.time_info.time_usable
                and table.time_info.column == override.column
                and table.time_info.unit == override.unit
            )
            if not already_matches:
                apply_time_override_to_file(
                    folder / table.source_file,
                    override.column,
                    override.unit,
                )
                applied_file_update = True

        if applied_file_update:
            res2 = ingest.ingest_folder(
                folder, workdir=effective_work, config=ingest_cfg
            )
            try:
                catalog = res2.catalog
            finally:
                res2.close()
    finally:
        if temp_work is not None:
            shutil.rmtree(temp_work, ignore_errors=True)

    statuses: list[TableTimeStatus] = []
    ready = True
    for table in catalog.tables:
        info = table.time_info
        needs_attention = (
            not info.time_usable
            or info.confidence < ingest_cfg.time_confidence_threshold
        )
        override = overrides_map.get(table.name)
        user_confirmed = bool(override is not None and override.confirmed)
        confirmed = (not needs_attention) or user_confirmed
        if not confirmed:
            ready = False
        statuses.append(
            TableTimeStatus(
                table_name=table.name,
                source_file=table.source_file,
                column=info.column,
                unit=info.unit,
                confidence=info.confidence,
                time_usable=info.time_usable,
                min_time_s=info.min_time_s,
                max_time_s=info.max_time_s,
                reason=info.reason,
                needs_attention=needs_attention,
                confirmed=confirmed,
                available_columns=tuple(
                    col.name
                    for col in table.columns
                    if col.name != '_time_unix_s'
                ),
            )
        )

    return IngestPreview(
        catalog=catalog,
        table_time_statuses=tuple(statuses),
        ready_for_time_analysis=ready,
    )


def inspect_runtime_status(
    *,
    models_path: pathlib.Path = DEFAULT_MODELS_PATH,
    budget_path: pathlib.Path = DEFAULT_BUDGET_PATH,
    agent_path: pathlib.Path = DEFAULT_AGENT_PATH,
    env_path: pathlib.Path = DEFAULT_ENV_PATH,
    spend_path: pathlib.Path = DEFAULT_SPEND_PATH,
    memory_path: pathlib.Path = DEFAULT_MEMORY_PATH,
) -> RuntimeStatus:
    """Inspect configured models, API keys, spend guard, and incident memory.

    Args:
        models_path: Path to `config/models.yaml`.
        budget_path: Path to `config/budget.yaml`.
        agent_path: Path to `config/agent.yaml`.
        env_path: Path to `.env`.
        spend_path: Path to SQLite spend ledger.
        memory_path: Path to SQLite incident memory database.

    Returns:
        Usable model options, spend guard amounts, and memory inventory.
    """
    env = llm_config.environment(env_path)
    models = llm_config.load_models(models_path)
    catalog = pricing.bundled_catalog()
    agent_cfg = agent_config.load_agent_config(agent_path)

    options: list[ModelOption] = []
    for alias in models.aliases:
        info = llm_client.model_info(models, env, catalog, alias)
        options.append(
            ModelOption(
                alias=alias,
                model_id=info.spec.id,
                provider=info.spec.provider,
                usable=info.usable,
                issues=info.issues,
            )
        )

    spend = spend_ledger.SpendLedger(
        spend_path,
        budget_path,
        env_override=env.get(llm_config.BUDGET_ENV),
    )
    snap = spend.snapshot()
    memory_records_count = 0
    if memory_path.is_file():
        with contextlib.suppress(sqlite3.Error, OSError, ValueError):
            memory_records_count = len(
                incident_memory.MemoryStore(memory_path, frozen=True).list_all()
            )
    return RuntimeStatus(
        models=tuple(options),
        usable_aliases=tuple(opt.alias for opt in options if opt.usable),
        default_alias=models.default,
        limit_usd=snap.limit,
        spent_usd=snap.spent,
        held_usd=snap.held,
        remaining_usd=snap.remaining,
        halted=snap.halted,
        per_case_cost_cap_usd=agent_cfg.limits.cost_usd,
        memory_enabled_default=agent_cfg.memory,
        memory_frozen=agent_cfg.memory_frozen,
        memory_records_count=memory_records_count,
    )


def _build_client(
    task: state.Task,
    settings: agent_config.AgentConfig,
    *,
    use_fake: bool,
    client_override: contracts.ModelClient | None,
    models_path: pathlib.Path,
    budget_path: pathlib.Path,
    env_path: pathlib.Path,
    spend_path: pathlib.Path,
) -> contracts.ModelClient:
    if client_override is not None:
        return client_override
    if use_fake:
        return fake_policy.RuleClient(task.candidate_services)
    env = llm_config.environment(env_path)
    models = llm_config.load_models(models_path)
    alias = settings.model_alias or models.default
    info = llm_client.model_info(models, env, pricing.bundled_catalog(), alias)
    if not info.usable:
        raise llm_config.ConfigError(f'{alias}: {"; ".join(info.issues)}')
    spend = spend_ledger.SpendLedger(
        spend_path,
        budget_path,
        env_override=env.get(llm_config.BUDGET_ENV),
    )
    return llm_client.GuardedClient(
        models, env, spend, litellm_transport.LiteLLMTransport()
    )


ClassificationOverrides = Mapping[
    str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
]


def _ingest_config(
    overrides: ClassificationOverrides | None,
) -> ingest_schema.IngestConfig | None:
    """Build an ingest config carrying user classification overrides."""
    if not overrides:
        return None
    return ingest_schema.IngestConfig(classification_overrides=overrides)


def _save_classification_overrides(
    run_dir: pathlib.Path, overrides: ClassificationOverrides | None
) -> None:
    """Persist overrides so a later continuation classifies files alike."""
    if overrides:
        run_state.write_json(
            run_dir / _OVERRIDES_FILE,
            {name: list(choice) for name, choice in overrides.items()},
        )


def load_classification_overrides(
    run_dir: pathlib.Path,
) -> dict[str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]]:
    """Load the overrides a run was started with, if any.

    Args:
        run_dir: Run folder.

    Returns:
        Overrides by file name; empty when none were saved or the file is
        unreadable or holds an invalid choice.
    """
    path = run_dir / _OVERRIDES_FILE
    if not path.is_file():
        return {}
    kinds = typing.get_args(ingest_schema.FileKind)
    strategies = typing.get_args(ingest_schema.ChunkStrategy)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    loaded: dict[
        str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
    ] = {}
    if not isinstance(raw, dict):
        return {}
    for name, choice in raw.items():
        if (
            isinstance(choice, list)
            and len(choice) == 2
            and choice[0] in kinds
            and choice[1] in strategies
        ):
            loaded[str(name)] = (choice[0], choice[1])
    return loaded


def _tail_trajectory(
    traj_path: pathlib.Path,
    future: futures.Future[graph.RunResult],
    on_event: Callable[[dict[str, object]], None] | None,
    *,
    start_seq: int = 0,
) -> None:
    emitted = start_seq
    while not future.done():
        if on_event is not None and traj_path.is_file():
            emitted = _emit_new_events(traj_path, emitted, on_event)
        time.sleep(0.02)
    if on_event is not None and traj_path.is_file():
        _emit_new_events(traj_path, emitted, on_event)


def _emit_new_events(
    traj_path: pathlib.Path,
    seen_count: int,
    on_event: Callable[[dict[str, object]], None],
) -> int:
    try:
        lines = traj_path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return seen_count
    for line in lines[seen_count:]:
        if not line.strip():
            continue
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            break
        if isinstance(event, dict):
            on_event(event)
            seen_count += 1
    return seen_count


def _preserve_initial_inputs(
    run_dir: pathlib.Path, folder: pathlib.Path
) -> None:
    """Copy staged initial inputs into `run_dir/inputs/c0` for continuation."""
    dest = run_dir / 'inputs' / 'c0'
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        for src in sorted(folder.rglob('*')):
            if src.is_dir() or src.suffix.lower() in ('.duckdb', '.wal'):
                continue
            target = dest / src.relative_to(folder)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    ckpt_path = run_dir / 'checkpoint.json'
    if ckpt_path.is_file():
        saved = json.loads(ckpt_path.read_text(encoding='utf-8'))
        if isinstance(saved, dict) and isinstance(saved.get('task'), dict):
            saved['task']['folder'] = str(dest)
            run_state.write_json(ckpt_path, saved)
    manifest_path = run_dir / 'manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if isinstance(manifest, dict) and isinstance(
            manifest.get('task'), dict
        ):
            manifest['task']['folder'] = str(dest)
            run_state.write_json(manifest_path, manifest)


def _apply_config_overrides(
    cfg: agent_config.AgentConfig,
    *,
    model_alias: str | None = None,
    agent_mode: Literal['single', 'multi'] | None = None,
    memory: bool | None = None,
) -> agent_config.AgentConfig:
    if model_alias:
        cfg = dataclasses.replace(cfg, model_alias=model_alias)
    if memory is not None:
        cfg = dataclasses.replace(cfg, memory=memory)
    if agent_mode is not None and any(
        field_info.name == 'mode' for field_info in dataclasses.fields(cfg)
    ):
        cfg = agent_config.AgentConfig.from_dict(
            {**cfg.to_dict(), 'mode': agent_mode}
        )
    return cfg


def _update_manifest_mode(run_dir: pathlib.Path, mode: str) -> None:
    manifest_path = run_dir / 'manifest.json'
    if not manifest_path.is_file():
        return
    with contextlib.suppress(OSError, ValueError):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if isinstance(manifest, dict):
            manifest['mode'] = mode
            run_state.write_json(manifest_path, manifest)


def _resolve_run_id(run_dir: pathlib.Path) -> str:
    for filename in ('manifest.json', 'checkpoint.json'):
        candidate = run_dir / filename
        if not candidate.is_file():
            continue
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(candidate.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('run_id'):
                return str(data['run_id'])
    return run_dir.name


def _build_memory_record_from_checkpoint(
    run_dir: pathlib.Path,
) -> incident_memory.MemoryRecord | None:
    ckpt_path = run_dir / 'checkpoint.json'
    if not ckpt_path.is_file():
        return None
    try:
        saved = json.loads(ckpt_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    raw_report = saved.get('report')
    raw_task = saved.get('task')
    if not isinstance(raw_report, dict) or not isinstance(raw_task, dict):
        return None
    try:
        final = report.Report.from_dict(raw_report)
        if not final.causes:
            return None
        task = state.Task.from_dict(raw_task)
        catalog = saved.get('catalog')
        catalog_map: Mapping[str, object] = (
            catalog if isinstance(catalog, Mapping) else {}
        )
        result_store = results.ResultStore.from_list(saved.get('results'))
    except (KeyError, TypeError, ValueError):
        return None
    return memory_record.build_record_from_run(
        task, catalog_map, final, result_store
    )


def run_app_investigation(
    task: state.Task,
    *,
    use_fake: bool = False,
    model_alias: str | None = None,
    agent_mode: Literal['single', 'multi'] | None = None,
    memory: bool | None = None,
    settings: agent_config.AgentConfig | None = None,
    runs_dir: pathlib.Path | None = None,
    client_override: contracts.ModelClient | None = None,
    classification_overrides: Mapping[
        str, tuple[ingest_schema.FileKind, ingest_schema.ChunkStrategy]
    ]
    | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    cleanup_session_folder: pathlib.Path | None = None,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: pathlib.Path | None = None,
    models_path: pathlib.Path = DEFAULT_MODELS_PATH,
    budget_path: pathlib.Path = DEFAULT_BUDGET_PATH,
    agent_path: pathlib.Path = DEFAULT_AGENT_PATH,
    env_path: pathlib.Path = DEFAULT_ENV_PATH,
    spend_path: pathlib.Path = DEFAULT_SPEND_PATH,
) -> graph.RunResult:
    """Run an investigation from the app, streaming trajectory events live.

    Args:
        task: Investigation task pointing to the staged folder.
        use_fake: When True, uses `fake_policy.RuleClient` at zero cost.
        model_alias: Optional model alias override.
        agent_mode: Optional `'single'` or `'multi'` mode override.
        memory: Optional override for enabling incident memory.
        settings: Optional pre-loaded `AgentConfig`.
        runs_dir: Destination parent directory for the run record.
        client_override: Optional injected `ModelClient` for tests.
        classification_overrides: Optional user file classification overrides.
        on_event: Optional callback receiving each trajectory event live.
        cleanup_session_folder: Optional temporary upload dir removed after run.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional explicit SQLite path for incident memory.
        models_path: Path to `config/models.yaml`.
        budget_path: Path to `config/budget.yaml`.
        agent_path: Path to `config/agent.yaml`.
        env_path: Path to `.env`.
        spend_path: Path to spend guard SQLite database.

    Returns:
        Completed `RunResult`.
    """
    cfg = _apply_config_overrides(
        settings or agent_config.load_agent_config(agent_path),
        model_alias=model_alias,
        agent_mode=agent_mode,
        memory=memory,
    )
    target_runs_dir = (
        runs_dir
        if runs_dir is not None
        else (DEFAULT_DRY_RUNS_DIR if use_fake else DEFAULT_RUNS_DIR)
    )
    run_id = graph.new_run_id(task)
    traj_path = target_runs_dir / run_id / 'trajectory.jsonl'

    try:
        model_client = _build_client(
            task,
            cfg,
            use_fake=use_fake,
            client_override=client_override,
            models_path=models_path,
            budget_path=budget_path,
            env_path=env_path,
            spend_path=spend_path,
        )

        def _execute() -> graph.RunResult:
            return graph.run_investigation(
                task,
                model_client,
                cfg,
                runs_dir=target_runs_dir,
                run_id=run_id,
                memory_store=memory_store,
                memory_path=memory_path,
                ingest_config=_ingest_config(classification_overrides),
            )

        with futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_execute)
            _tail_trajectory(traj_path, future, on_event)
            result = future.result()

        if result.run_dir is not None:
            _save_classification_overrides(
                result.run_dir, classification_overrides
            )
            _preserve_initial_inputs(result.run_dir, task.folder)
            if agent_mode is not None:
                _update_manifest_mode(result.run_dir, agent_mode)
        return result
    finally:
        cleanup_session_dir(cleanup_session_folder)


def preview_continuation(
    run_dir: pathlib.Path,
    added_files: Sequence[pathlib.Path],
) -> ContinuationPreview:
    """Preview catalog changes and estimated cost before continuing a run.

    Args:
        run_dir: Existing run folder with `checkpoint.json`.
        added_files: New files or directories to add.

    Returns:
        Catalog diff lines and estimated per-continuation cost cap.

    Raises:
        FileNotFoundError: When checkpoint or an added file is missing.
        ValueError: When the run has not finished or a filename collides.
    """
    if not added_files:
        raise ValueError('Add at least one file to continue the run')
    saved = json.loads((run_dir / 'checkpoint.json').read_text('utf-8'))
    if saved.get('report') is None:
        raise ValueError(f'{run_dir} has not finished; nothing to continue')
    earlier = state.Task.from_dict(saved['task'])
    settings = agent_config.AgentConfig.from_dict(saved['config'])
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix='incidents-cont-preview-'))
    try:
        combined = tmp_root / 'inputs'
        names = continuation._combine(earlier.folder, added_files, combined)
        ingested = ingest.ingest_folder(
            combined,
            workdir=tmp_root / 'db',
            config=_ingest_config(load_classification_overrides(run_dir)),
        )
        try:
            summary = graph.catalog_summary(ingested.catalog)
        finally:
            ingested.close()
        changes = continuation.what_changed(saved.get('catalog') or {}, summary)
        return ContinuationPreview(
            added_files=tuple(names),
            changes=tuple(changes),
            estimated_cost_usd=settings.limits.cost_usd,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def continue_app_run(
    run_dir: pathlib.Path,
    added_files: Sequence[pathlib.Path],
    *,
    use_fake: bool = False,
    model_alias: str | None = None,
    agent_mode: Literal['single', 'multi'] | None = None,
    memory: bool | None = None,
    client_override: contracts.ModelClient | None = None,
    on_event: Callable[[dict[str, object]], None] | None = None,
    cleanup_added_dir: pathlib.Path | None = None,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: pathlib.Path | None = None,
    models_path: pathlib.Path = DEFAULT_MODELS_PATH,
    budget_path: pathlib.Path = DEFAULT_BUDGET_PATH,
    env_path: pathlib.Path = DEFAULT_ENV_PATH,
    spend_path: pathlib.Path = DEFAULT_SPEND_PATH,
) -> graph.RunResult:
    """Continue a finished run with added files and stream new events.

    Args:
        run_dir: Run directory holding `checkpoint.json`.
        added_files: Added file paths.
        use_fake: When True, uses `RuleClient` at zero cost.
        model_alias: Optional model alias override for this continuation pass.
        agent_mode: Optional `'single'` or `'multi'` mode override.
        memory: Optional override for enabling incident memory.
        client_override: Optional injected `ModelClient` for tests.
        on_event: Optional callback receiving newly appended events.
        cleanup_added_dir: Optional temporary folder holding added uploads.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional explicit SQLite path for incident memory.
        models_path: Path to `config/models.yaml`.
        budget_path: Path to `config/budget.yaml`.
        env_path: Path to `.env`.
        spend_path: Path to spend guard SQLite database.

    Returns:
        Updated `RunResult` for the continuation pass.

    Raises:
        ValueError: When `run_dir` is evaluation output.
    """
    if is_eval_output(run_dir, DEFAULT_RUNS_DIR):
        raise ValueError(f'{run_dir} is evaluation output, not an app run')
    saved = json.loads((run_dir / 'checkpoint.json').read_text('utf-8'))
    earlier = state.Task.from_dict(saved['task'])
    settings = _apply_config_overrides(
        agent_config.AgentConfig.from_dict(saved['config']),
        model_alias=model_alias,
        agent_mode=agent_mode,
        memory=memory,
    )
    prev_report = (
        report.Report.from_dict(saved['report'])
        if isinstance(saved.get('report'), dict)
        else None
    )
    prev_feedback = load_feedback(
        run_dir, memory_store=memory_store, memory_path=memory_path
    )
    traj_path = run_dir / 'trajectory.jsonl'
    existing_count = (
        len(traj_path.read_text(encoding='utf-8').splitlines())
        if traj_path.is_file()
        else 0
    )

    try:
        model_client = _build_client(
            earlier,
            settings,
            use_fake=use_fake,
            client_override=client_override,
            models_path=models_path,
            budget_path=budget_path,
            env_path=env_path,
            spend_path=spend_path,
        )
        with futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                continuation.continue_run,
                run_dir,
                added_files,
                model_client,
                settings,
                memory_store=memory_store,
                memory_path=memory_path,
                ingest_config=_ingest_config(
                    load_classification_overrides(run_dir)
                ),
            )
            _tail_trajectory(
                traj_path, future, on_event, start_seq=existing_count
            )
            result = future.result()
        if (
            isinstance(result, graph.RunResult)
            and prev_feedback is not None
            and (run_dir / 'feedback.json').is_file()
        ):
            prev_cause = (
                prev_report.ranks[0]
                if prev_report is not None
                and prev_report.outcome != 'no_fault_found'
                and prev_report.ranks
                else None
            )
            new_cause = (
                result.report.ranks[0]
                if result.report.outcome != 'no_fault_found'
                and result.report.ranks
                else None
            )
            carried_outcome: Literal['confirmed', 'rejected', 'unverified'] = (
                prev_feedback.outcome
                if prev_cause == new_cause
                else 'unverified'
            )
            carried = dataclasses.replace(
                prev_feedback,
                continuation=result.report.continuation,
                outcome=carried_outcome,
                recorded_at=datetime.datetime.now(datetime.UTC).isoformat(),
            )
            run_state.write_json(run_dir / 'feedback.json', carried.to_dict())
        return result
    finally:
        cleanup_session_dir(cleanup_added_dir)


def save_feedback(
    run_dir: pathlib.Path,
    feedback: FeedbackRecord,
    *,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: pathlib.Path | None = None,
) -> pathlib.Path:
    """Save operator feedback locally and sync it to incident memory.

    Args:
        run_dir: Run folder.
        feedback: Feedback record to persist.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional explicit SQLite path for incident memory.

    Returns:
        Path to the saved feedback JSON file.
    """
    stamped = (
        feedback
        if feedback.recorded_at
        else dataclasses.replace(
            feedback,
            recorded_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
    )
    path = run_dir / 'feedback.json'
    run_state.write_json(path, stamped.to_dict())

    target_store = memory_store
    if target_store is None:
        target_path = memory_path or (run_dir.parent / 'memory.sqlite3')
        if target_path.is_file() or (run_dir / 'checkpoint.json').is_file():
            target_store = incident_memory.MemoryStore(target_path)

    if target_store is not None:
        with contextlib.suppress(
            incident_memory.FrozenMemoryError, sqlite3.Error, OSError
        ):
            existing = target_store.get(stamped.run_id)
            if existing is None:
                built = _build_memory_record_from_checkpoint(run_dir)
                if built is not None:
                    if built.run_id != stamped.run_id:
                        built = dataclasses.replace(
                            built, run_id=stamped.run_id
                        )
                    target_store.save(built)
                    existing = built
            if existing is not None:
                target_store.update_outcome(
                    stamped.run_id,
                    stamped.outcome,
                    what_fixed_it=stamped.what_fixed_it.strip() or None,
                    updated_at=stamped.recorded_at,
                )
    return path


def load_feedback(
    run_dir: pathlib.Path,
    *,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: pathlib.Path | None = None,
) -> FeedbackRecord | None:
    """Load operator feedback from `feedback.json` or incident memory.

    Args:
        run_dir: Run folder.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional explicit SQLite path for incident memory.

    Returns:
        Latest feedback record if found, else None.
    """
    local_fb: FeedbackRecord | None = None
    path = run_dir / 'feedback.json'
    if path.is_file():
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            local_fb = FeedbackRecord.from_dict(data)

    run_id = (
        local_fb.run_id
        if local_fb is not None and local_fb.run_id
        else _resolve_run_id(run_dir)
    )
    read_store = memory_store
    if read_store is None:
        target_path = memory_path or (run_dir.parent / 'memory.sqlite3')
        if target_path.is_file():
            read_store = incident_memory.MemoryStore(target_path, frozen=True)

    mem_rec: incident_memory.MemoryRecord | None = None
    if read_store is not None:
        with contextlib.suppress(sqlite3.Error, OSError, ValueError):
            mem_rec = read_store.get(run_id)

    if mem_rec is not None:
        if local_fb is None:
            return FeedbackRecord(
                run_id=mem_rec.run_id,
                continuation=0,
                outcome=mem_rec.outcome,
                actual_root_cause=mem_rec.diagnosed_root_cause or '',
                what_fixed_it=mem_rec.what_fixed_it or '',
                notes='',
                recorded_at=mem_rec.updated_at,
            )
        if mem_rec.updated_at > local_fb.recorded_at:
            return dataclasses.replace(
                local_fb,
                outcome=mem_rec.outcome,
                what_fixed_it=mem_rec.what_fixed_it or local_fb.what_fixed_it,
                recorded_at=mem_rec.updated_at,
            )
    return local_fb


def is_eval_output(path: pathlib.Path, runs_root: pathlib.Path) -> bool:
    """Tell whether `path` is inside evaluation output under `runs_root`.

    Args:
        path: File or folder to test.
        runs_root: Root runs folder shared with the evaluation runner.

    Returns:
        True for `runs/phase-NN/...` and any `cache` or `attempts` folder.
    """
    try:
        parts = path.resolve().relative_to(runs_root.resolve()).parts
    except ValueError:
        return False
    if not parts:
        return False
    return parts[0].startswith(_EVAL_ROOT_PREFIX) or bool(
        _EVAL_CACHE_PARTS.intersection(parts)
    )


def delete_run(
    run_dir: pathlib.Path,
    *,
    runs_root: pathlib.Path = DEFAULT_RUNS_DIR,
    memory_deleter: Callable[[str], None] | None = None,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: pathlib.Path | None = None,
) -> None:
    """Delete a saved run folder and remove any matching memory record.

    Args:
        run_dir: Run folder to remove.
        runs_root: Parent runs directory that must contain `run_dir`.
        memory_deleter: Optional callback receiving `run_id`.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional explicit SQLite path for incident memory.

    Raises:
        ValueError: When `run_dir` is outside `runs_root`, equals it, or is
            evaluation output.
        FileNotFoundError: When `run_dir` does not exist.
    """
    resolved_root = runs_root.resolve()
    resolved_run = run_dir.resolve()
    if resolved_run == resolved_root or not resolved_run.is_relative_to(
        resolved_root
    ):
        raise ValueError(f'{run_dir} is not inside {runs_root}')
    if is_eval_output(resolved_run, resolved_root):
        raise ValueError(f'{run_dir} is evaluation output, not an app run')
    if not resolved_run.is_dir():
        raise FileNotFoundError(f'Run directory not found: {run_dir}')

    run_id = _resolve_run_id(resolved_run)

    if memory_deleter is not None:
        memory_deleter(run_id)
    if memory_store is not None:
        with contextlib.suppress(incident_memory.FrozenMemoryError):
            memory_store.delete(run_id)
    else:
        candidates = (
            [memory_path]
            if memory_path is not None
            else sorted(
                {
                    resolved_run.parent / 'memory.sqlite3',
                    resolved_root / 'memory.sqlite3',
                }
            )
        )
        for candidate in candidates:
            if candidate.is_file():
                with contextlib.suppress(
                    incident_memory.FrozenMemoryError,
                    sqlite3.Error,
                    OSError,
                ):
                    incident_memory.MemoryStore(candidate).delete(run_id)
    shutil.rmtree(resolved_run)


@functools.lru_cache(maxsize=512)
def _summarize_trajectory(
    path: str, mtime_ns: int, size: int
) -> PastRunSummary | None:
    """Summarize one trajectory; cached per (path, mtime, size).

    Streamlit reruns the script on every interaction, so parsing every saved
    trajectory each time would grow with the history. The key changes when the
    file does, so stale summaries are never served.

    Args:
        path: Trajectory file path.
        mtime_ns: Modification time of the file, in nanoseconds.
        size: File size in bytes.

    Returns:
        The summary, or None when the file is not a replayable agent run.
    """
    del size  # Only part of the cache key.
    traj_path = pathlib.Path(path)
    try:
        events = trajectory.read_trajectory(traj_path)
    except (OSError, ValueError):
        return None
    finals = [
        event
        for event in events
        if event.get('v') == trajectory.TRAJECTORY_VERSION
        and event.get('type') == 'final_report'
        and isinstance(event.get('report'), dict)
    ]
    if not finals:
        return None
    latest_body = finals[-1]['report']
    assert isinstance(latest_body, dict)
    try:
        latest_report = report.Report.from_dict(latest_body)
    except (KeyError, ValueError):
        return None
    continuations = tuple(
        sorted({int(str(event.get('continuation', 0))) for event in finals})
    )
    top_cause = latest_report.ranks[0] if latest_report.ranks else None
    return PastRunSummary(
        run_id=latest_report.run_id,
        run_dir=traj_path.parent,
        trajectory_path=traj_path,
        outcome=latest_report.outcome,
        top_cause=top_cause,
        continuations=continuations,
        updated_at=mtime_ns / 1e9,
    )


def list_past_runs(
    runs_dir: pathlib.Path = DEFAULT_RUNS_DIR,
) -> list[PastRunSummary]:
    """List saved agent trajectories under `runs_dir`, newest first.

    Non-agent trajectories (such as `check-models-*` spend logs without v1
    `final_report` events) and evaluation output (`runs/phase-NN/`, cached
    attempts) are skipped, so held-out results never appear in the viewer.

    Args:
        runs_dir: Root runs folder to scan.

    Returns:
        Summaries of replayable runs.
    """
    if not runs_dir.is_dir():
        return []
    summaries: list[PastRunSummary] = []
    for traj_path in sorted(runs_dir.rglob('trajectory.jsonl')):
        if is_eval_output(traj_path, runs_dir):
            continue
        try:
            stat = traj_path.stat()
        except OSError:
            continue
        summary = _summarize_trajectory(
            str(traj_path), stat.st_mtime_ns, stat.st_size
        )
        if summary is not None:
            summaries.append(summary)
    summaries.sort(key=lambda item: item.updated_at, reverse=True)
    return summaries


def list_published_runs(
    bundle: pathlib.Path = REPO_ROOT / 'results/final',
) -> list[PastRunSummary]:
    """List only replayable trajectories explicitly named in the bundle."""
    index = bundle / 'replay.json'
    if not index.is_file():
        return []
    data = json.loads(index.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Unsupported published replay index')
    paths = data.get('trajectories')
    if not isinstance(paths, list):
        raise ValueError('Invalid published replay index')
    root = (bundle / 'trajectories').resolve()
    if not root.is_relative_to(bundle.resolve()):
        raise ValueError('Published trajectories must stay inside the bundle')
    summaries = []
    seen = set()
    for entry in paths:
        if not isinstance(entry, str) or pathlib.Path(entry).is_absolute():
            raise ValueError('Published trajectory must use a relative path')
        path = (bundle / entry).resolve()
        if not path.is_relative_to(root) or path.name != 'trajectory.jsonl':
            raise ValueError('Published trajectory path escapes its directory')
        if path in seen:
            raise ValueError('Duplicate published trajectory')
        seen.add(path)
        stat = path.stat()
        summary = _summarize_trajectory(
            str(path), stat.st_mtime_ns, stat.st_size
        )
        if summary is not None:
            summaries.append(summary)
    return summaries


def load_past_run(
    trajectory_path: pathlib.Path,
    continuation_index: int | None = None,
) -> LoadedPastRun:
    """Replay a saved trajectory from disk without calling a model.

    Args:
        trajectory_path: Path to `trajectory.jsonl`.
        continuation_index: Optional continuation pass; defaults to latest.

    Returns:
        Loaded report, validated citation count, DuckDB path, and feedback.

    Raises:
        ValueError: When the trajectory has no final report for the pass.
    """
    events = trajectory.read_trajectory(trajectory_path)
    finals = [
        event
        for event in events
        if event.get('type') == 'final_report'
        and isinstance(event.get('report'), dict)
    ]
    if not finals:
        raise ValueError(f'{trajectory_path} has no final report')
    available = tuple(
        sorted({int(str(event.get('continuation', 0))) for event in finals})
    )
    if continuation_index is None:
        chosen_event = finals[-1]
    else:
        matching = [
            event
            for event in finals
            if int(str(event.get('continuation', 0))) == continuation_index
        ]
        if not matching:
            raise ValueError(
                f'{trajectory_path} has no final report for continuation '
                f'{continuation_index}'
            )
        chosen_event = matching[-1]
    body = chosen_event['report']
    if not isinstance(body, dict):
        raise ValueError(f'{trajectory_path} has a malformed final report')
    final = report.Report.from_dict(body)

    store = results.ResultStore()
    for event in events:
        if (
            event.get('type') == 'tool_call'
            and event.get('call_id') not in store
        ):
            store.add(results.StoredResult.from_dict(event))

    cited = [item for cause in final.causes for item in cause.evidence]
    valid = sum(
        agent_ledger.check_citation(
            item.to_dict(), store, final.run_id, default_stance=item.stance
        )[0]
        is not None
        for item in cited
    )

    run_dir = trajectory_path.parent
    db_candidate = (
        run_dir / f'db-c{final.continuation}' / 'incident.duckdb'
        if final.continuation > 0
        else run_dir / 'db' / 'incident.duckdb'
    )
    db_path = db_candidate if db_candidate.is_file() else None
    if db_path is None and (run_dir / 'db' / 'incident.duckdb').is_file():
        db_path = run_dir / 'db' / 'incident.duckdb'

    fault_start: float | None = None
    manifest_path = run_dir / 'manifest.json'
    if manifest_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if isinstance(manifest, dict) and isinstance(
                manifest.get('task'), dict
            ):
                raw_start = manifest['task'].get('fault_start_s')
                if isinstance(raw_start, (int, float)) and not isinstance(
                    raw_start, bool
                ):
                    fault_start = float(raw_start)

    return LoadedPastRun(
        run_id=final.run_id,
        run_dir=run_dir,
        trajectory_path=trajectory_path,
        continuation=final.continuation,
        available_continuations=available,
        report=final,
        events=tuple(events),
        valid_citations=valid,
        total_citations=len(cited),
        db_path=db_path,
        task_fault_start_s=fault_start,
        feedback=load_feedback(run_dir),
    )
