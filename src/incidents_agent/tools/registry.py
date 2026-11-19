"""Tool registry, argument schema specifications, and call-log auditing."""

import json
import time

import duckdb

from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.memory import store as incident_memory
from incidents_agent.tools import documents
from incidents_agent.tools import graph
from incidents_agent.tools import logs
from incidents_agent.tools import metrics
from incidents_agent.tools import safety
from incidents_agent.tools import schema as tool_schema
from incidents_agent.tools import tables


class ToolRegistry:
    """Registry dispatching typed tool calls with logging and safety caps."""

    def __init__(
        self, max_result_chars: int = 6_000, first_call_number: int = 1
    ) -> None:
        """Initialise an empty registry with a result cap.

        Args:
            max_result_chars: Character cap applied to every result.
            first_call_number: Number of the first call id, so a run that
                continues with a new registry never reuses an id.
        """
        if first_call_number < 1:
            raise ValueError('first_call_number must be at least 1')
        self._tools: dict[str, tool_schema.ToolSpec] = {}
        self._call_log: list[tool_schema.ToolCallRecord] = []
        self._max_result_chars = max_result_chars
        self._first_call_number = first_call_number

    def register(self, spec: tool_schema.ToolSpec) -> None:
        """Register or replace a tool specification by name."""
        self._tools[spec.name] = spec

    def names(self) -> tuple[str, ...]:
        """Return registered tool names in registration order."""
        return tuple(self._tools.keys())

    def schemas(self) -> list[dict[str, object]]:
        """Return OpenAI/LiteLLM function schemas for all registered tools."""
        return [spec.to_openai_schema() for spec in self._tools.values()]

    @property
    def call_log(self) -> tuple[tool_schema.ToolCallRecord, ...]:
        """Return an immutable snapshot of all tool calls recorded so far."""
        return tuple(self._call_log)

    def get_call_result_id(self) -> str:
        """Return the next sequential call identifier without incrementing."""
        return self._call_id()

    def _call_id(self) -> str:
        return f'call_{len(self._call_log) + self._first_call_number:04d}'

    def call(
        self,
        name: str,
        args: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Execute a named tool safely, returning JSON-serialisable data.

        Never raises an exception to the caller; errors are returned as
        `{"ok": False, "error": {...}}`. Output values pass through pattern-
        based secret redaction and the character size cap before logging.

        Args:
            name: Registered tool name.
            args: Optional dictionary of arguments passed to the tool handler.

        Returns:
            JSON-serialisable tool response dictionary.
        """
        started = time.monotonic()
        call_id = self._call_id()
        safe_args: dict[str, object] = (
            dict(args) if isinstance(args, dict) else {}
        )

        spec = self._tools.get(name)
        if spec is None:
            raw_result: dict[str, object] = tables.error_result(
                'unknown_tool', f'Unknown tool: {name}'
            )
        elif args is not None and not isinstance(args, dict):
            raw_result = tables.error_result(
                'invalid_args', 'Tool arguments must be a dictionary'
            )
        else:
            try:
                raw_result = spec.handler(safe_args)
            except (
                duckdb.Error,
                ValueError,
                TypeError,
                KeyError,
                OSError,
                RuntimeError,
            ) as exc:
                raw_result = tables.error_result('tool_exception', str(exc))

        raw_targets = raw_result.pop('target_components', [])
        targets = (
            tuple(str(item) for item in raw_targets)
            if isinstance(raw_targets, (list, tuple))
            else ()
        )

        raw_result['call_id'] = call_id
        raw_result['tool'] = name

        sanitised_obj, redacted_count = safety.redact_secrets(raw_result)
        sanitised = (
            sanitised_obj
            if isinstance(sanitised_obj, dict)
            else {'ok': False, 'error': {'code': 'internal', 'message': 'bad'}}
        )
        sanitised['redacted_secrets_count'] = redacted_count

        capped = safety.cap_result_size(
            sanitised, max_chars=self._max_result_chars
        )
        serialised_size = len(json.dumps(capped, ensure_ascii=False))
        duration_ms = round((time.monotonic() - started) * 1000.0, 3)

        raw_locator = capped.get('locator')
        locator_dict = raw_locator if isinstance(raw_locator, dict) else None
        ok = bool(capped.get('ok', False))

        self._call_log.append(
            tool_schema.ToolCallRecord(
                call_id=call_id,
                tool_name=name,
                args=safe_args,
                duration_ms=duration_ms,
                result_size_chars=serialised_size,
                ok=ok,
                redacted_secrets_count=redacted_count,
                target_components=targets,
                locator=locator_dict,
            )
        )
        return capped


def create_default_registry(
    ingest_result: ingest_schema.IngestResult,
    first_call_number: int = 1,
    *,
    memory_store: incident_memory.MemoryStore | None = None,
    run_id: str | None = None,
) -> ToolRegistry:
    """Create a ToolRegistry wired with all Phase 03 tools for `ingest_result`.

    Args:
        ingest_result: Result from `incidents_agent.ingest.ingest_folder`.
        first_call_number: Number of the first call id (see `ToolRegistry`).
        memory_store: Optional incident memory store; when provided, registers
            `search_past_incidents`.
        run_id: Current run id excluded from memory matches.

    Returns:
        Configured ToolRegistry enforcing `ingest_result.catalog.limits`.
    """
    ctx = tables.ToolContext(
        connection=ingest_result.connection,
        catalog=ingest_result.catalog,
        db_path=ingest_result.db_path,
    )
    registry = ToolRegistry(
        max_result_chars=ingest_result.catalog.limits.max_result_chars,
        first_call_number=first_call_number,
    )

    specs: tuple[tool_schema.ToolSpec, ...] = (
        tool_schema.ToolSpec(
            name='list_tables',
            description=(
                'List ingested tables, row counts, time usability, '
                'relationships, documents, and skipped files.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {},
                'additionalProperties': False,
            },
            handler=lambda args: tables.list_tables(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='describe_table',
            description=(
                'Return column types, summary stats, time mapping, '
                'relationships, and sample rows for a table. Columns come '
                'a page at a time (default 20); filter them with '
                'column_pattern and page with summary.next_column_offset.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'table': {'type': 'string'},
                    'sample_rows': {'type': 'integer', 'minimum': 0},
                    'column_pattern': {'type': 'string'},
                    'column_offset': {'type': 'integer', 'minimum': 0},
                    'max_columns': {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': 50,
                    },
                },
                'required': ['table'],
                'additionalProperties': False,
            },
            handler=lambda args: tables.describe_table(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='run_sql',
            description=(
                'Execute a read-only single SELECT SQL query against DuckDB.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
            handler=lambda args: tables.run_sql(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='search_text',
            description=(
                'Regex search over a text column grouped by masked message '
                'pattern, with optional before/after counts at split_time.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'table': {'type': 'string'},
                    'pattern': {'type': 'string'},
                    'column': {'type': 'string'},
                    'split_time': {'type': 'number'},
                    'top_k': {'type': 'integer', 'minimum': 1},
                },
                'required': ['table', 'pattern'],
                'additionalProperties': False,
            },
            handler=lambda args: logs.search_text(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='compare_windows',
            description=(
                'Rank numeric metric columns by robust shift across split_time '
                '(for tables with numeric columns; use search_text or run_sql '
                'for log/event/object tables).'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'table': {'type': 'string'},
                    'split_time': {'type': 'number'},
                    'top_k': {'type': 'integer', 'minimum': 1},
                    'columns': {
                        'type': 'array',
                        'items': {'type': 'string'},
                    },
                    'group_by': {'type': 'string'},
                },
                'required': ['table', 'split_time'],
                'additionalProperties': False,
            },
            handler=lambda args: metrics.compare_windows(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='time_series',
            description=(
                'Downsample one numeric column into at most num_buckets '
                'time buckets.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'table': {'type': 'string'},
                    'column': {'type': 'string'},
                    'num_buckets': {'type': 'integer', 'minimum': 1},
                    'start_time': {'type': 'number'},
                    'end_time': {'type': 'number'},
                    'agg': {
                        'type': 'string',
                        'enum': ['avg', 'min', 'max', 'sum', 'count'],
                    },
                },
                'required': ['table', 'column'],
                'additionalProperties': False,
            },
            handler=lambda args: metrics.time_series(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='find_onset',
            description=(
                'Estimate incident start timestamp from metric change points '
                'and log error bursts.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'tables': {
                        'type': 'array',
                        'items': {'type': 'string'},
                    },
                    'num_buckets': {'type': 'integer', 'minimum': 10},
                },
                'additionalProperties': False,
            },
            handler=lambda args: metrics.find_onset(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='search_documents',
            description=(
                'Keyword BM25 search across ingested document chunks.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'top_k': {'type': 'integer', 'minimum': 1},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
            handler=lambda args: documents.search_documents(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='read_document',
            description=(
                'Read a document chunk with neighbouring chunks by chunk_id, '
                'or read a document outline by doc_id.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'chunk_id': {'type': 'string'},
                    'doc_id': {'type': 'string'},
                    'neighbors': {'type': 'integer', 'minimum': 0},
                },
                'additionalProperties': False,
            },
            handler=lambda args: documents.read_document(ctx, args),
        ),
        tool_schema.ToolSpec(
            name='dependency_graph',
            description=(
                'Build a component call graph with before/after counts, error '
                'rates, and latencies from detected relationship columns.'
            ),
            parameters_schema={
                'type': 'object',
                'properties': {
                    'split_time': {'type': 'number'},
                    'component': {'type': 'string'},
                    'table': {'type': 'string'},
                },
                'additionalProperties': False,
            },
            handler=lambda args: graph.dependency_graph(ctx, args),
        ),
    )
    for spec in specs:
        registry.register(spec)
    if memory_store is not None:
        store_ref = memory_store
        registry.register(
            tool_schema.ToolSpec(
                name=incident_memory.SEARCH_TOOL_NAME,
                description=(
                    'Search past incidents for confirmed leads, rejected '
                    'diagnoses (counterevidence), and unchecked priors.'
                ),
                parameters_schema={
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string'},
                        'components': {
                            'type': 'array',
                            'items': {'type': 'string'},
                        },
                        'top_k': {'type': 'integer', 'minimum': 1},
                    },
                    'required': ['query'],
                    'additionalProperties': False,
                },
                handler=lambda args: incident_memory.search_past_incidents(
                    store_ref, args, exclude_run_id=run_id
                ),
            )
        )
    return registry
