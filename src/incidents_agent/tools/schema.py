"""Typed schemas for tool specifications, locators, and call log records."""

import dataclasses
from collections.abc import Callable
from typing import Literal

LocatorKind = Literal[
    'catalog',
    'table_schema',
    'sql_query',
    'text_search',
    'window_comparison',
    'time_series',
    'document_chunks',
    'document_outline',
    'onset_estimate',
    'dependency_graph',
]


@dataclasses.dataclass(frozen=True)
class ToolLocator:
    """Stable locator describing the data behind a tool result.

    Attributes:
        kind: Category of query or source the result came from.
        table: Target DuckDB table name, when applicable.
        columns: Column names read or ranked by the tool.
        chunk_ids: Document chunk identifiers returned or cited.
        window: Optional `(start_s, end_s)` or split-time window metadata.
        expression: Optional summary expression, SQL query, or search pattern.
    """

    kind: LocatorKind
    table: str | None = None
    columns: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    window: tuple[float | None, float | None] | None = None
    expression: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable locator dictionary."""
        result: dict[str, object] = {'kind': self.kind}
        if self.table is not None:
            result['table'] = self.table
        if self.columns:
            result['columns'] = list(self.columns)
        if self.chunk_ids:
            result['chunk_ids'] = list(self.chunk_ids)
        if self.window is not None:
            result['window'] = [self.window[0], self.window[1]]
        if self.expression is not None:
            result['expression'] = self.expression
        return result


@dataclasses.dataclass(frozen=True)
class ToolSpec:
    """Registered tool definition combining JSON schema and Python handler.

    Attributes:
        name: Unique tool name exposed to the agent and registry.
        description: Human- and model-readable summary of what the tool does.
        parameters_schema: JSON Schema object describing accepted arguments.
        handler: Typed callable accepting an arguments dict and returning
            raw payload, locator, and target components or an error dict.
    """

    name: str
    description: str
    parameters_schema: dict[str, object]
    handler: Callable[[dict[str, object]], dict[str, object]]

    def to_openai_schema(self) -> dict[str, object]:
        """Return an OpenAI/LiteLLM function-calling tool schema dictionary."""
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self.description,
                'parameters': self.parameters_schema,
            },
        }


@dataclasses.dataclass(frozen=True)
class ToolCallRecord:
    """Audit record captured for every tool invocation in a run.

    Attributes:
        call_id: Deterministic sequential call identifier (`call_0001`, ...).
        tool_name: Name of the tool invoked.
        args: Arguments dictionary supplied to the call.
        duration_ms: Wall-clock duration of the tool execution in milliseconds.
        result_size_chars: Character length of the serialised final response.
        ok: True if the tool succeeded without returning an error.
        redacted_secrets_count: Count of secret matches masked in the output.
        target_components: Factual component or service names touched/returned.
        locator: Serialised locator dictionary when the tool succeeded.
    """

    call_id: str
    tool_name: str
    args: dict[str, object]
    duration_ms: float
    result_size_chars: int
    ok: bool
    redacted_secrets_count: int
    target_components: tuple[str, ...]
    locator: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation of this call record."""
        return {
            'call_id': self.call_id,
            'tool_name': self.tool_name,
            'args': self.args,
            'duration_ms': self.duration_ms,
            'result_size_chars': self.result_size_chars,
            'ok': self.ok,
            'redacted_secrets_count': self.redacted_secrets_count,
            'target_components': list(self.target_components),
            'locator': self.locator,
        }
