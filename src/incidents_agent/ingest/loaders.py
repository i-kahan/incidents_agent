"""Load dropped incident files into a read-only DuckDB database and catalog."""

import datetime
import decimal
import math
import pathlib
import re
import tempfile
from collections.abc import Sequence

import duckdb

from incidents_agent.ingest import chunks
from incidents_agent.ingest import classifier
from incidents_agent.ingest import relationships
from incidents_agent.ingest import schema
from incidents_agent.ingest import time_detect

_IGNORED_NAMES = frozenset({'.DS_Store', 'Thumbs.db'})
_IGNORED_DIRS = frozenset({'.git', '__pycache__', '.pytest_cache'})

_TS_PREFIX_RE = re.compile(
    r'^\s*\[?('
    r'\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?'
    r'|\d{10,19}(?:\.\d+)?'
    r')\]?'
)
_LEVEL_RE = re.compile(
    r'(?:^|\s|\[)(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|CRIT)'
    r'(?:\]|:|\s|$)',
    re.IGNORECASE,
)


def derive_table_name(relative_path: str, used_names: set[str]) -> str:
    """Derive a unique, collision-free DuckDB table name from a relative path.

    Args:
        relative_path: POSIX-style path relative to the ingested folder root.
        used_names: Mutable set of table names already claimed in this ingest.

    Returns:
        Sanitised SQL table identifier, registered in `used_names`.
    """
    path = pathlib.PurePosixPath(relative_path)
    parts = list(path.parts[:-1]) + [path.stem]
    cleaned_parts = [
        re.sub(r'[^a-zA-Z0-9]+', '_', part).strip('_').lower() for part in parts
    ]
    non_empty = [part for part in cleaned_parts if part]
    base = '__'.join(non_empty) if non_empty else 'table'
    if base[0].isdigit() or base == 'document_chunks':
        base = f't_{base}'

    candidate = base
    suffix = 2
    while candidate in used_names:
        candidate = f'{base}_{suffix}'
        suffix += 1
    used_names.add(candidate)
    return candidate


def json_safe(value: object) -> object:
    """Convert DuckDB row values into JSON-serialisable Python primitives."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)


def _iter_folder_files(
    folder: pathlib.Path,
) -> tuple[list[tuple[pathlib.Path, str]], list[schema.SkippedFileEntry]]:
    """Walk `folder` deterministically, returning regular files and skips."""
    root = folder.resolve()
    files: list[tuple[pathlib.Path, str]] = []
    skipped: list[schema.SkippedFileEntry] = []

    for candidate in sorted(folder.rglob('*')):
        rel = candidate.relative_to(folder).as_posix()
        parts = set(candidate.relative_to(folder).parts)
        if parts & _IGNORED_DIRS or candidate.name in _IGNORED_NAMES:
            continue
        if candidate.is_dir():
            continue
        if candidate.suffix.lower() in ('.duckdb', '.wal'):
            continue
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            skipped.append(schema.SkippedFileEntry(rel, f'unreadable: {exc}'))
            continue
        if not resolved.is_relative_to(root):
            skipped.append(
                schema.SkippedFileEntry(rel, 'symlink escapes input folder')
            )
            continue
        if not candidate.is_file():
            skipped.append(schema.SkippedFileEntry(rel, 'not a regular file'))
            continue
        files.append((candidate, rel))

    return (files, skipped)


def _describe_columns(
    conn: duckdb.DuckDBPyConnection, table_name: str
) -> tuple[schema.ColumnInfo, ...]:
    """Read DuckDB column metadata for `table_name`."""
    table_sql = time_detect.quote_ident(table_name)
    rows = conn.execute(f'DESCRIBE {table_sql}').fetchall()
    return tuple(
        schema.ColumnInfo(name=str(row[0]), dtype=str(row[1])) for row in rows
    )


def _read_sample_rows(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    columns: Sequence[schema.ColumnInfo],
    limit: int = 3,
) -> tuple[dict[str, object], ...]:
    """Fetch up to `limit` JSON-safe sample rows from `table_name`."""
    table_sql = time_detect.quote_ident(table_name)
    col_names = [col.name for col in columns]
    rows = conn.execute(f'SELECT * FROM {table_sql} LIMIT {limit}').fetchall()
    samples: list[dict[str, object]] = []
    for row in rows:
        samples.append(
            {
                col_name: json_safe(val)
                for col_name, val in zip(col_names, row, strict=True)
            }
        )
    return tuple(samples)


def _load_tabular_file(
    conn: duckdb.DuckDBPyConnection,
    file_path: pathlib.Path,
    table_name: str,
) -> None:
    """Load CSV, TSV, Parquet, JSONL, or JSON-array file into DuckDB."""
    table_sql = time_detect.quote_ident(table_name)
    ext = file_path.suffix.lower()
    if ext == '.csv':
        sql = (
            f'CREATE TABLE {table_sql} AS '
            'SELECT * FROM read_csv_auto(?, header=true, ignore_errors=false)'
        )
    elif ext == '.tsv':
        sql = (
            f'CREATE TABLE {table_sql} AS '
            "SELECT * FROM read_csv_auto(?, delim='\t', header=true, "
            'ignore_errors=false)'
        )
    elif ext == '.parquet':
        sql = f'CREATE TABLE {table_sql} AS SELECT * FROM read_parquet(?)'
    elif ext in ('.jsonl', '.ndjson'):
        sql = (
            f'CREATE TABLE {table_sql} AS '
            "SELECT * FROM read_json_auto(?, format='newline_delimited')"
        )
    elif ext == '.json':
        sql = (
            f'CREATE TABLE {table_sql} AS '
            "SELECT * FROM read_json_auto(?, format='array')"
        )
    else:
        raise ValueError(f'unsupported tabular extension: {ext}')

    conn.execute(sql, [str(file_path)])
    for desc_row in conn.execute(f'DESCRIBE {table_sql}').fetchall():
        col_name = str(desc_row[0])
        col_type = str(desc_row[1]).upper()
        if 'WITH TIME ZONE' in col_type or col_type == 'TIMESTAMPTZ':
            col_sql = time_detect.quote_ident(col_name)
            conn.execute(
                f'ALTER TABLE {table_sql} ALTER COLUMN {col_sql} '
                f"SET DATA TYPE TIMESTAMP USING ({col_sql} AT TIME ZONE 'UTC')"
            )


def _parse_log_line(
    line_no: int, raw_line: str
) -> tuple[int, str, str | None, str | None]:
    """Extract optional leading timestamp and severity level from a log line."""
    text = raw_line.rstrip('\r\n')
    ts_match = _TS_PREFIX_RE.match(text)
    timestamp = ts_match.group(1) if ts_match else None
    search_window = text[ts_match.end() :] if ts_match else text
    level_match = _LEVEL_RE.search(search_window[:64])
    level = level_match.group(1).upper() if level_match else None
    return (line_no, text, timestamp, level)


def _load_log_file(
    conn: duckdb.DuckDBPyConnection,
    file_path: pathlib.Path,
    table_name: str,
) -> None:
    """Load a plain-text log file into a structured DuckDB log table."""
    table_sql = time_detect.quote_ident(table_name)
    conn.execute(
        f'CREATE TABLE {table_sql} ('
        'line_no BIGINT, '
        'raw_text VARCHAR, '
        'timestamp VARCHAR, '
        'level VARCHAR'
        ')'
    )
    rows: list[tuple[int, str, str | None, str | None]] = []
    with file_path.open('r', encoding='utf-8') as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            rows.append(_parse_log_line(line_no, raw_line))
    if rows:
        conn.executemany(
            f'INSERT INTO {table_sql} VALUES (?, ?, ?, ?)',
            rows,
        )


def _load_document_file(
    conn: duckdb.DuckDBPyConnection,
    file_path: pathlib.Path,
    classification: schema.FileClassification,
    limits: schema.IngestLimits,
) -> schema.DocumentCatalogEntry:
    """Chunk a supporting document and store it in document_chunks."""
    text = file_path.read_text(encoding='utf-8')
    doc_id = classification.relative_path
    title = chunks.derive_document_title(classification.relative_path, text)
    doc_chunks = chunks.chunk_document(
        doc_id=doc_id,
        title=title,
        text=text,
        strategy=classification.chunk_strategy,
        limits=limits,
    )
    if doc_chunks:
        conn.executemany(
            'INSERT INTO document_chunks VALUES (?, ?, ?, ?, ?)',
            [
                (
                    chunk.doc_id,
                    chunk.chunk_id,
                    chunk.position,
                    chunk.heading_path,
                    chunk.text,
                )
                for chunk in doc_chunks
            ],
        )
    outline = tuple(
        schema.DocumentOutlineItem(
            chunk_id=chunk.chunk_id,
            position=chunk.position,
            heading_path=chunk.heading_path,
        )
        for chunk in doc_chunks
    )
    doc_type = file_path.suffix.lstrip('.').lower() or 'text'
    return schema.DocumentCatalogEntry(
        doc_id=doc_id,
        source_file=classification.relative_path,
        title=title,
        doc_type=doc_type,
        chunk_strategy=classification.chunk_strategy,
        size_bytes=classification.size_bytes,
        chunk_count=len(doc_chunks),
        outline=outline,
    )


def open_readonly_connection(
    db_path: pathlib.Path,
) -> duckdb.DuckDBPyConnection:
    """Open an existing DuckDB file read-only with external access disabled.

    Args:
        db_path: Filesystem path to the DuckDB database file.

    Returns:
        Read-only DuckDBPyConnection with external file and network access off.
    """
    conn = duckdb.connect(str(db_path), read_only=True)
    conn.execute('SET enable_external_access = false')
    conn.execute('SET lock_configuration = true')
    return conn


def ingest_folder(
    folder: pathlib.Path,
    workdir: pathlib.Path | None = None,
    config: schema.IngestConfig | None = None,
) -> schema.IngestResult:
    """Ingest a folder of incident files into a read-only DuckDB database.

    Args:
        folder: Directory containing tables, logs, and/or supporting documents.
        workdir: Optional output directory where `incident.duckdb` is written.
            Defaults to a fresh temporary directory.
        config: Optional ingest limits, overrides, and fallback classifier.

    Returns:
        IngestResult holding the read-only DuckDB connection and built Catalog.

    Raises:
        FileNotFoundError: When `folder` does not exist or is not a directory.
    """
    if not folder.is_dir():
        raise FileNotFoundError(f'Input folder does not exist: {folder}')

    effective_config = config or schema.IngestConfig()
    limits = effective_config.limits
    target_dir = (
        workdir
        if workdir is not None
        else pathlib.Path(tempfile.mkdtemp(prefix='incidents-db-'))
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / 'incident.duckdb'
    if db_path.exists():
        db_path.unlink()
    wal_path = pathlib.Path(f'{db_path}.wal')
    if wal_path.exists():
        wal_path.unlink()

    discovered, skipped = _iter_folder_files(folder)
    used_names: set[str] = {'document_chunks'}
    classifications: list[schema.FileClassification] = []
    tables: list[schema.TableCatalogEntry] = []
    documents: list[schema.DocumentCatalogEntry] = []
    limits_hit: list[str] = []

    accepted_files = 0
    total_bytes = 0

    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS document_chunks ('
            'doc_id VARCHAR, '
            'chunk_id VARCHAR, '
            'position INTEGER, '
            'heading_path VARCHAR, '
            'text VARCHAR'
            ')'
        )

        for file_path, rel_path in discovered:
            try:
                size_bytes = file_path.stat().st_size
            except OSError as exc:
                skipped.append(
                    schema.SkippedFileEntry(rel_path, f'stat failed: {exc}')
                )
                continue

            if size_bytes > limits.max_file_bytes:
                skipped.append(
                    schema.SkippedFileEntry(
                        rel_path,
                        f'exceeds max_file_bytes ({size_bytes} > '
                        f'{limits.max_file_bytes})',
                        size_bytes,
                    )
                )
                if 'max_file_bytes' not in limits_hit:
                    limits_hit.append('max_file_bytes')
                continue

            if accepted_files >= limits.max_files:
                skipped.append(
                    schema.SkippedFileEntry(
                        rel_path,
                        f'exceeds max_files ({limits.max_files})',
                        size_bytes,
                    )
                )
                if 'max_files' not in limits_hit:
                    limits_hit.append('max_files')
                continue

            if total_bytes + size_bytes > limits.max_total_bytes:
                skipped.append(
                    schema.SkippedFileEntry(
                        rel_path,
                        f'exceeds max_total_bytes '
                        f'({total_bytes + size_bytes} > '
                        f'{limits.max_total_bytes})',
                        size_bytes,
                    )
                )
                if 'max_total_bytes' not in limits_hit:
                    limits_hit.append('max_total_bytes')
                continue

            try:
                cls = classifier.classify_file(
                    path=file_path,
                    relative_path=rel_path,
                    config=effective_config,
                )
            except (classifier.UnsupportedFileError, OSError) as exc:
                skipped.append(
                    schema.SkippedFileEntry(rel_path, str(exc), size_bytes)
                )
                continue

            if cls.kind == 'document':
                try:
                    doc_entry = _load_document_file(
                        conn=conn,
                        file_path=file_path,
                        classification=cls,
                        limits=limits,
                    )
                except (OSError, UnicodeDecodeError, duckdb.Error) as exc:
                    skipped.append(
                        schema.SkippedFileEntry(
                            rel_path,
                            f'document load failed: {exc}',
                            size_bytes,
                        )
                    )
                    continue
                documents.append(doc_entry)
                classifications.append(cls)
                accepted_files += 1
                total_bytes += size_bytes
                continue

            table_name = derive_table_name(rel_path, used_names)
            table_sql = time_detect.quote_ident(table_name)
            try:
                if cls.kind == 'tabular':
                    _load_tabular_file(conn, file_path, table_name)
                else:
                    _load_log_file(conn, file_path, table_name)
            except (
                duckdb.Error,
                OSError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                conn.execute(f'DROP TABLE IF EXISTS {table_sql}')
                skipped.append(
                    schema.SkippedFileEntry(
                        rel_path,
                        f'{cls.kind} load failed: {exc}',
                        size_bytes,
                    )
                )
                continue

            init_cols = _describe_columns(conn, table_name)
            time_info = time_detect.detect_and_normalise_time(
                conn=conn,
                table_name=table_name,
                columns=init_cols,
                confidence_threshold=effective_config.time_confidence_threshold,
            )
            final_cols = _describe_columns(conn, table_name)
            rels = relationships.detect_relationships(
                conn=conn,
                table_name=table_name,
                columns=final_cols,
            )
            count_row = conn.execute(
                f'SELECT COUNT(*) FROM {table_sql}'
            ).fetchone()
            row_count = int(count_row[0]) if count_row is not None else 0
            sample_rows = _read_sample_rows(conn, table_name, final_cols)
            table_kind: schema.FileKind = cls.kind
            if table_kind not in ('tabular', 'log'):
                continue
            tables.append(
                schema.TableCatalogEntry(
                    name=table_name,
                    source_file=rel_path,
                    kind=table_kind,
                    row_count=row_count,
                    columns=final_cols,
                    time_info=time_info,
                    relationships=rels,
                    sample_rows=sample_rows,
                )
            )
            classifications.append(cls)
            accepted_files += 1
            total_bytes += size_bytes
    finally:
        conn.close()

    catalog = schema.Catalog(
        tables=tuple(tables),
        documents=tuple(documents),
        classifications=tuple(classifications),
        skipped_files=tuple(skipped),
        limits=limits,
        limits_hit=tuple(limits_hit),
    )
    ro_conn = open_readonly_connection(db_path)
    return schema.IngestResult(
        db_path=db_path,
        connection=ro_conn,
        catalog=catalog,
    )
