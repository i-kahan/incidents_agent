"""Run data tools for the agent and keep their full results citable."""

import json
import threading
from collections.abc import Mapping

from incidents_agent.agent import config
from incidents_agent.agent import results
from incidents_agent.llm import contracts
from incidents_agent.tools import registry as tool_registry

READ_RESULT_NAME = 'read_result'

READ_RESULT_TOOL = contracts.Tool(
    READ_RESULT_NAME,
    'Show the full result of an earlier data-tool call in this run, for '
    'example one replaced by a digest. Cite it by its original call id.',
    {
        'type': 'object',
        'properties': {'call_id': {'type': 'string'}},
        'required': ['call_id'],
        'additionalProperties': False,
    },
)


def json_schema(schema: Mapping[str, object]) -> dict[str, contracts.JSON]:
    """Return a JSON-typed copy of a tool parameter schema."""
    copied: dict[str, contracts.JSON] = json.loads(json.dumps(schema))
    return copied


def as_tools(
    schemas: list[dict[str, object]],
) -> tuple[contracts.Tool, ...]:
    """Convert OpenAI-style function schemas to model-layer tools."""
    tools: list[contracts.Tool] = []
    for schema in schemas:
        function = schema.get('function')
        if not isinstance(function, dict):
            continue
        parameters = json.loads(json.dumps(function.get('parameters') or {}))
        tools.append(
            contracts.Tool(
                str(function['name']),
                str(function.get('description', '')),
                parameters,
            )
        )
    return tuple(tools)


class ToolExecutor:
    """Call registry tools and store each full result under its call id.

    Calls are serialised behind one lock, so roles running on threads
    (Phase 07) never share a call id and the run's single DuckDB connection
    is used by one thread at a time.
    """

    def __init__(
        self,
        registry: tool_registry.ToolRegistry,
        store: results.ResultStore,
        run_id: str,
        continuation: int = 0,
    ) -> None:
        """Wrap a registry for one run (and one continuation of it)."""
        self._registry = registry
        self.store = store
        self.run_id = run_id
        self.continuation = continuation
        self.tools = as_tools(registry.schemas())
        self._lock = threading.Lock()

    def names(self) -> frozenset[str]:
        """Return the data-tool names."""
        return frozenset(tool.name for tool in self.tools)

    def call(
        self, name: str, args: dict[str, object] | None
    ) -> results.StoredResult:
        """Run one data tool; the registry never raises on tool errors.

        Args:
            name: Tool name.
            args: Parsed arguments, or None when they did not parse.

        Returns:
            The stored result, also added to the run's store.
        """
        with self._lock:
            result = self._registry.call(name, args)
            record = self._registry.call_log[-1]
            stored = results.StoredResult(
                call_id=record.call_id,
                run_id=self.run_id,
                tool=name,
                args=dict(record.args),
                ok=record.ok,
                result=result,
                target_components=record.target_components,
                locator=record.locator,
                continuation=self.continuation,
            )
            self.store.add(stored)
        return stored

    def read(self, call_id: object) -> dict[str, object]:
        """Return a stored result in full, or an error result."""
        stored = self.store.get(str(call_id))
        if stored is None:
            return {
                'ok': False,
                'error': {
                    'code': 'unknown_call',
                    'message': f'No data-tool call {call_id!r} in this run',
                },
            }
        return dict(stored.result)


def digest(stored: results.StoredResult, policy: config.ContextPolicy) -> str:
    """Return the short, deterministic stand-in for an older result.

    Args:
        stored: The full result.
        policy: Lines and characters to keep.

    Returns:
        Tool, arguments, locator, size, the first lines, and how to re-read.
    """
    body = json.dumps(
        results.payload(stored.result), indent=1, ensure_ascii=False
    )
    lines = [
        line[: policy.digest_line_chars]
        for line in body.splitlines()[1 : policy.digest_lines + 1]
    ]
    header = {
        'digest_of': stored.call_id,
        'tool': stored.tool,
        'args': stored.args,
        'ok': stored.ok,
        'locator': stored.locator,
        'size_chars': len(json.dumps(stored.result, ensure_ascii=False)),
    }
    return '\n'.join(
        [
            json.dumps(header, ensure_ascii=False, default=str),
            *lines,
            f'(Digest. The full result is kept: read_result call_id='
            f'"{stored.call_id}". Cite it by this call id.)',
        ]
    )


def result_text(result: Mapping[str, object]) -> str:
    """Serialise a tool result for the conversation."""
    return json.dumps(result, ensure_ascii=False, default=str)
