"""Data models and configuration schemas for Phase 03 ingest."""

import dataclasses
import decimal
import pathlib
from collections.abc import Mapping
from typing import Literal

import duckdb

from incidents_agent.llm import contracts

FileKind = Literal['tabular', 'log', 'document']
ChunkStrategy = Literal[
    'none',
    'headings',
    'paragraphs',
    'hunks',
    'sections',
    'whole',
]
TimeUnit = Literal[
    'unix_s',
    'unix_ms',
    'unix_us',
    'unix_ns',
    'iso8601',
    'unknown',
]
RelationshipKind = Literal['caller_callee', 'span_parent']


@dataclasses.dataclass(frozen=True)
class IngestLimits:
    """Configurable size, count, query, and chunking limits for ingest.

    Attributes:
        max_file_bytes: Maximum allowed size of a single input file in bytes.
        max_files: Maximum number of files ingested in one folder.
        max_total_bytes: Maximum combined size of ingested files in bytes.
        max_query_rows: Maximum rows returned by a single SQL query tool call.
        max_scan_rows: Maximum rows scanned for in-Python tool operations.
        query_timeout_s: Maximum execution duration per DuckDB query.
        max_result_chars: Character cap applied to serialized tool outputs.
        whole_doc_max_chars: Documents at or below this length stay whole.
        max_chunk_chars: Maximum character length of a single document chunk.
        chunk_overlap_chars: Character overlap when splitting oversized chunks.
    """

    max_file_bytes: int = 500 * 1024 * 1024
    max_files: int = 200
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_query_rows: int = 500
    max_scan_rows: int = 2_000_000
    query_timeout_s: float = 5.0
    max_result_chars: int = 6_000
    whole_doc_max_chars: int = 1_200
    max_chunk_chars: int = 1_500
    chunk_overlap_chars: int = 150

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'max_file_bytes': self.max_file_bytes,
            'max_files': self.max_files,
            'max_total_bytes': self.max_total_bytes,
            'max_query_rows': self.max_query_rows,
            'max_scan_rows': self.max_scan_rows,
            'query_timeout_s': self.query_timeout_s,
            'max_result_chars': self.max_result_chars,
            'whole_doc_max_chars': self.whole_doc_max_chars,
            'max_chunk_chars': self.max_chunk_chars,
            'chunk_overlap_chars': self.chunk_overlap_chars,
        }


@dataclasses.dataclass(frozen=True)
class ModelClassifierDecision:
    """Classification decision returned by an optional model fallback client.

    Attributes:
        kind: Chosen file kind from the fixed menu.
        chunk_strategy: Chosen chunking strategy from the fixed menu.
        reason: Short explanation of why the model selected this option.
        cost_usd: Spend recorded for the model classification call.
    """

    kind: str
    chunk_strategy: str
    reason: str
    cost_usd: decimal.Decimal = decimal.Decimal(0)


@dataclasses.dataclass(frozen=True)
class IngestConfig:
    """Runtime configuration controlling deterministic and fallback ingest.

    Attributes:
        limits: Size, count, query, and chunking limits.
        enable_model_classification: Enables the model fallback when true.
        classifier_confidence_threshold: Minimum rule confidence to skip model.
        time_confidence_threshold: Minimum time detection confidence required.
        classifier_client: Optional Phase 04 ModelClient for fallback calls.
        classifier_model_alias: Model alias passed in ChatRequest (empty uses
            the configured default model).
        classifier_run: Run identity recorded against SpendLedger reservations.
        classifier_case_id: Optional case identifier for per-case spend caps.
        classifier_max_output_tokens: Output token cap for fallback calls.
        classifier_cache: Optional mutable cache keyed by SHA-256 file hash.
        classification_overrides: Optional per-path user overrides.
    """

    limits: IngestLimits = dataclasses.field(default_factory=IngestLimits)
    enable_model_classification: bool = False
    classifier_confidence_threshold: float = 0.70
    time_confidence_threshold: float = 0.70
    classifier_client: contracts.ModelClient | None = None
    classifier_model_alias: str = ''
    classifier_run: contracts.Run = dataclasses.field(
        default_factory=lambda: contracts.Run('ingest')
    )
    classifier_case_id: str | None = None
    classifier_max_output_tokens: int = 96
    classifier_cache: dict[str, ModelClassifierDecision] | None = None
    classification_overrides: (
        Mapping[str, tuple[FileKind, ChunkStrategy]] | None
    ) = None


@dataclasses.dataclass(frozen=True)
class FileClassification:
    """Recorded file classification decision for a single input file.

    Attributes:
        relative_path: File path relative to the ingested folder root.
        file_hash: Hexadecimal SHA-256 digest of the file contents.
        size_bytes: File size in bytes.
        kind: Assigned file kind (tabular, log, or document).
        chunk_strategy: Assigned chunking strategy for documents, else 'none'.
        confidence: Confidence score in [0.0, 1.0].
        reason: Human-readable explanation of how the classification was chosen.
        used_model_fallback: True if the optional model fallback decided it.
        model_cost_usd: Cost in USD attributed to classifying this file.
        fallback_halted: True if a budget, billing, or model error halted
            subsequent model fallback calls for this ingest run.
    """

    relative_path: str
    file_hash: str
    size_bytes: int
    kind: FileKind
    chunk_strategy: ChunkStrategy
    confidence: float
    reason: str
    used_model_fallback: bool = False
    model_cost_usd: decimal.Decimal = decimal.Decimal(0)
    fallback_halted: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'relative_path': self.relative_path,
            'file_hash': self.file_hash,
            'size_bytes': self.size_bytes,
            'kind': self.kind,
            'chunk_strategy': self.chunk_strategy,
            'confidence': self.confidence,
            'reason': self.reason,
            'used_model_fallback': self.used_model_fallback,
            'model_cost_usd': str(self.model_cost_usd),
        }


@dataclasses.dataclass(frozen=True)
class ColumnInfo:
    """Schema metadata for a single column in a DuckDB table.

    Attributes:
        name: Column name in DuckDB.
        dtype: DuckDB SQL type string.
    """

    name: str
    dtype: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {'name': self.name, 'dtype': self.dtype}


@dataclasses.dataclass(frozen=True)
class TimeColumnInfo:
    """Time-column detection and normalisation summary for a table.

    Attributes:
        column: Name of the detected original time column, if any.
        normalised_column: Name of the added Unix-seconds column, if usable.
        unit: Inferred source timestamp unit.
        timezone_assumption: Recorded timezone assumption (e.g. 'UTC').
        confidence: Confidence score in [0.0, 1.0].
        min_time_s: Minimum normalised Unix timestamp in seconds, if usable.
        max_time_s: Maximum normalised Unix timestamp in seconds, if usable.
        time_usable: True if time-window tools may query this table.
        reason: Explanation of the detection result or why time is unusable.
    """

    column: str | None
    normalised_column: str | None
    unit: TimeUnit
    timezone_assumption: str
    confidence: float
    min_time_s: float | None
    max_time_s: float | None
    time_usable: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'column': self.column,
            'normalised_column': self.normalised_column,
            'unit': self.unit,
            'timezone_assumption': self.timezone_assumption,
            'confidence': self.confidence,
            'min_time_s': self.min_time_s,
            'max_time_s': self.max_time_s,
            'time_usable': self.time_usable,
            'reason': self.reason,
        }


@dataclasses.dataclass(frozen=True)
class RelationshipInfo:
    """Detected directed or hierarchical relationship columns in a table.

    Attributes:
        kind: Either 'caller_callee' or 'span_parent'.
        source_column: Caller or child span ID column.
        target_column: Callee or parent span ID column.
        component_column: Component/service column when kind is 'span_parent'.
        duration_column: Optional latency or duration column name.
        status_column: Optional status or error column name.
        confidence: Confidence score in [0.0, 1.0].
        overlap_ratio: Observed value overlap supporting the relationship.
        reason: Explanation of how the relationship was identified.
    """

    kind: RelationshipKind
    source_column: str
    target_column: str
    component_column: str | None
    duration_column: str | None
    status_column: str | None
    confidence: float
    overlap_ratio: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'kind': self.kind,
            'source_column': self.source_column,
            'target_column': self.target_column,
            'component_column': self.component_column,
            'duration_column': self.duration_column,
            'status_column': self.status_column,
            'confidence': self.confidence,
            'overlap_ratio': self.overlap_ratio,
            'reason': self.reason,
        }


@dataclasses.dataclass(frozen=True)
class TableCatalogEntry:
    """Catalog entry describing a single loaded tabular or log table.

    Attributes:
        name: Unique DuckDB table name.
        source_file: Original file path relative to the folder root.
        kind: Either 'tabular' or 'log'.
        row_count: Total rows loaded into the table.
        columns: Column schemas in table order.
        time_info: Time detection and normalisation status.
        relationships: Detected relationship descriptors, or empty tuple.
        sample_rows: Up to three sample rows from the table.
    """

    name: str
    source_file: str
    kind: Literal['tabular', 'log']
    row_count: int
    columns: tuple[ColumnInfo, ...]
    time_info: TimeColumnInfo
    relationships: tuple[RelationshipInfo, ...]
    sample_rows: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'name': self.name,
            'source_file': self.source_file,
            'kind': self.kind,
            'row_count': self.row_count,
            'columns': [col.to_dict() for col in self.columns],
            'time_info': self.time_info.to_dict(),
            'relationships': [rel.to_dict() for rel in self.relationships],
            'sample_rows': list(self.sample_rows),
        }


@dataclasses.dataclass(frozen=True)
class DocumentChunk:
    """A structure-derived chunk extracted from an ingested document.

    Attributes:
        doc_id: Stable identifier for the parent document.
        chunk_id: Globally unique chunk identifier for citations.
        position: Zero-based chunk ordinal within the parent document.
        heading_path: Section, heading, or hunk path locating the chunk.
        text: Chunk text content.
    """

    doc_id: str
    chunk_id: str
    position: int
    heading_path: str
    text: str


@dataclasses.dataclass(frozen=True)
class DocumentOutlineItem:
    """One outline entry mapping a chunk id to its heading or section path.

    Attributes:
        chunk_id: Chunk identifier.
        position: Zero-based ordinal within the document.
        heading_path: Heading or section label for this chunk.
    """

    chunk_id: str
    position: int
    heading_path: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'chunk_id': self.chunk_id,
            'position': self.position,
            'heading_path': self.heading_path,
        }


@dataclasses.dataclass(frozen=True)
class DocumentCatalogEntry:
    """Catalog entry describing a chunked supporting document.

    Attributes:
        doc_id: Unique document identifier.
        source_file: Original file path relative to the folder root.
        title: Document title derived from heading or file path.
        doc_type: Document extension or format label.
        chunk_strategy: Chunking strategy applied.
        size_bytes: Original file size in bytes.
        chunk_count: Number of chunks stored in document_chunks.
        outline: Ordered sequence of outline entries for the document.
    """

    doc_id: str
    source_file: str
    title: str
    doc_type: str
    chunk_strategy: ChunkStrategy
    size_bytes: int
    chunk_count: int
    outline: tuple[DocumentOutlineItem, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'doc_id': self.doc_id,
            'source_file': self.source_file,
            'title': self.title,
            'doc_type': self.doc_type,
            'chunk_strategy': self.chunk_strategy,
            'size_bytes': self.size_bytes,
            'chunk_count': self.chunk_count,
            'outline': [item.to_dict() for item in self.outline],
        }


@dataclasses.dataclass(frozen=True)
class SkippedFileEntry:
    """Record of an input file that was skipped during ingest.

    Attributes:
        relative_path: Path relative to the ingested folder root.
        reason: Human-readable reason the file was skipped.
        size_bytes: File size in bytes when stat succeeded.
    """

    relative_path: str
    reason: str
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'relative_path': self.relative_path,
            'reason': self.reason,
            'size_bytes': self.size_bytes,
        }


@dataclasses.dataclass(frozen=True)
class Catalog:
    """Complete catalog produced by ingesting an incident folder.

    Attributes:
        tables: Metadata for every loaded tabular or log table.
        documents: Metadata for every chunked supporting document.
        classifications: Classification decisions recorded during ingest.
        skipped_files: Files skipped because of limits, errors, or type.
        limits: Ingest limits active during the run.
        limits_hit: Descriptions of any limits reached during ingest.
    """

    tables: tuple[TableCatalogEntry, ...]
    documents: tuple[DocumentCatalogEntry, ...]
    classifications: tuple[FileClassification, ...]
    skipped_files: tuple[SkippedFileEntry, ...]
    limits: IngestLimits
    limits_hit: tuple[str, ...] = ()

    def get_table(self, name: str) -> TableCatalogEntry | None:
        """Return the table catalog entry matching name, or None."""
        for table in self.tables:
            if table.name == name:
                return table
        return None

    def get_document(self, doc_id: str) -> DocumentCatalogEntry | None:
        """Return the document catalog entry matching doc_id, or None."""
        for doc in self.documents:
            if doc.doc_id == doc_id:
                return doc
        return None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dictionary representation."""
        return {
            'tables': [table.to_dict() for table in self.tables],
            'documents': [doc.to_dict() for doc in self.documents],
            'classifications': [cls.to_dict() for cls in self.classifications],
            'skipped_files': [skip.to_dict() for skip in self.skipped_files],
            'limits': self.limits.to_dict(),
            'limits_hit': list(self.limits_hit),
        }


@dataclasses.dataclass
class IngestResult:
    """Result handle holding a read-only DuckDB connection and its catalog.

    Attributes:
        db_path: Path to the persisted DuckDB file.
        connection: Read-only DuckDB connection with external access disabled.
        catalog: Structured catalog describing all ingested tables and docs.
    """

    db_path: pathlib.Path
    connection: duckdb.DuckDBPyConnection
    catalog: Catalog

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        self.connection.close()
