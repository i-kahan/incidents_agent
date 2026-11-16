"""Full tool results kept for a run, so citations stay checkable."""

import dataclasses
import json
import re
from collections.abc import Iterator
from collections.abc import Mapping

# Tools whose results come from past incidents rather than the current data
# (Phase 09). Evidence citing them never counts as support from this data.
MEMORY_TOOLS = frozenset({'search_past_incidents'})

# Keys the registry and the size cap add; they describe a result, they are
# not observations, so they are never searched for quotes.
_META_KEYS = frozenset(
    {
        'ok',
        'call_id',
        'tool',
        'locator',
        'redacted_secrets_count',
        'truncated',
        'truncation',
        'truncation_notice',
        'summary',
        'error',
    }
)
# Keys whose values identify a row, so they can serve as a locator.
_ROW_KEYS = frozenset(
    {
        'name',
        'table',
        'column',
        'chunk_id',
        'doc_id',
        'caller',
        'callee',
        'component',
        'service',
        'serviceName',
        'pattern_template',
        'incident_id',
        'diagnosed_root_cause',
        'rejected_root_cause',
    }
)
_CALL_NUMBER = re.compile(r'call_(\d+)$')


@dataclasses.dataclass(frozen=True)
class StoredResult:
    """One data-tool call and the full result the model was shown.

    Attributes:
        call_id: Run-scoped call id (`call_0001`, ...).
        run_id: Run the call belongs to.
        tool: Tool name.
        args: Arguments the call was made with.
        ok: True when the tool succeeded.
        result: Full result after redaction and the size cap.
        target_components: Components the call filtered on or returned.
        locator: The result's locator dictionary, if any.
        continuation: Continuation index the call was made in.
    """

    call_id: str
    run_id: str
    tool: str
    args: Mapping[str, object]
    ok: bool
    result: Mapping[str, object]
    target_components: tuple[str, ...] = ()
    locator: Mapping[str, object] | None = None
    continuation: int = 0

    @property
    def from_memory(self) -> bool:
        """Return True for results about past incidents."""
        return self.tool in MEMORY_TOOLS

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return {
            'call_id': self.call_id,
            'run_id': self.run_id,
            'tool': self.tool,
            'args': dict(self.args),
            'ok': self.ok,
            'result': dict(self.result),
            'target_components': list(self.target_components),
            'locator': None if self.locator is None else dict(self.locator),
            'continuation': self.continuation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'StoredResult':
        """Rebuild a record written by `to_dict`."""
        args = data.get('args')
        result = data.get('result')
        locator = data.get('locator')
        targets = data.get('target_components')
        return cls(
            call_id=str(data['call_id']),
            run_id=str(data['run_id']),
            tool=str(data['tool']),
            args=dict(args) if isinstance(args, dict) else {},
            ok=bool(data['ok']),
            result=dict(result) if isinstance(result, dict) else {},
            target_components=tuple(
                str(item)
                for item in (targets if isinstance(targets, list) else [])
            ),
            locator=dict(locator) if isinstance(locator, dict) else None,
            continuation=int(str(data.get('continuation', 0))),
        )


class ResultStore:
    """Data-tool results of one run, keyed by call id."""

    def __init__(self) -> None:
        """Start empty."""
        self._items: dict[str, StoredResult] = {}

    def add(self, item: StoredResult) -> None:
        """Store a result; call ids are never reused within a store."""
        if item.call_id in self._items:
            raise ValueError(f'Duplicate call id {item.call_id}')
        self._items[item.call_id] = item

    def get(self, call_id: str) -> StoredResult | None:
        """Return the stored result for a call id, or None."""
        return self._items.get(call_id)

    def __contains__(self, call_id: object) -> bool:
        """Return True when the call id is stored."""
        return call_id in self._items

    def __len__(self) -> int:
        """Return the number of stored results."""
        return len(self._items)

    def __iter__(self) -> Iterator[StoredResult]:
        """Iterate in call order."""
        return iter(self._items.values())

    def next_call_number(self) -> int:
        """Return the number the next call id should use."""
        numbers = [
            int(match.group(1))
            for call_id in self._items
            if (match := _CALL_NUMBER.match(call_id))
        ]
        return max(numbers, default=0) + 1

    def to_list(self) -> list[dict[str, object]]:
        """Return every stored result, JSON-ready, in call order."""
        return [item.to_dict() for item in self._items.values()]

    @classmethod
    def from_list(cls, items: object) -> 'ResultStore':
        """Rebuild a store written by `to_list`."""
        store = cls()
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                store.add(StoredResult.from_dict(item))
        return store


def payload(result: Mapping[str, object]) -> dict[str, object]:
    """Return the observations in a result, without metadata or envelopes.

    The untrusted-data envelope is unwrapped to its content, so its fixed
    notice is never mistaken for an observation.

    Args:
        result: A tool result as returned by the registry.

    Returns:
        The result's observation fields.
    """
    body: dict[str, object] = {}
    for key, value in result.items():
        if key in _META_KEYS:
            continue
        if key == 'untrusted_data' and isinstance(value, dict):
            body['content'] = value.get('content')
        else:
            body[key] = value
    return body


def _leaves(value: object) -> Iterator[tuple[str | None, object]]:
    stack: list[tuple[str | None, object]] = [(None, value)]
    while stack:
        key, node = stack.pop()
        if isinstance(node, dict):
            for child_key, child in node.items():
                stack.append((str(child_key), child))
        elif isinstance(node, list | tuple):
            stack.extend((key, child) for child in node)
        else:
            yield key, node


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def is_empty(result: Mapping[str, object]) -> bool:
    """Return True when a result carries no observation at all."""
    return not any(
        (text := _text(value)) is not None and text.strip()
        for _, value in _leaves(payload(result))
    )


def _normalise(text: str) -> str:
    return ' '.join(text.casefold().split())


def contains_quote(result: Mapping[str, object], quote: str) -> bool:
    """Return True when `quote` appears in one observation of the result.

    Matching ignores case and runs of whitespace. Keys count as well as
    values, so a column name can be quoted.

    Args:
        result: A tool result.
        quote: The quoted value or phrase.

    Returns:
        Whether the quote appears.
    """
    wanted = _normalise(quote)
    if len(wanted.replace(' ', '')) < 2:
        return False
    for key, value in _leaves(payload(result)):
        if key is not None and wanted in _normalise(key):
            return True
        text = _text(value)
        if text is not None and wanted in _normalise(text):
            return True
    return False


def locator_tokens(item: StoredResult) -> set[str]:
    """Return the strings a citation may use to locate data in a result.

    They are the locator's table, columns, chunk ids, expression and window
    bounds, plus row identifiers (table, column, chunk, component names)
    found in the result.

    Args:
        item: A stored result.

    Returns:
        Accepted locator strings.
    """
    tokens: set[str] = set()
    locator = item.locator or {}
    for key in ('table', 'expression'):
        value = locator.get(key)
        if isinstance(value, str) and value:
            tokens.add(value)
    for key in ('columns', 'chunk_ids', 'window'):
        values = locator.get(key)
        if isinstance(values, list):
            tokens.update(
                text for value in values if (text := _text(value)) is not None
            )
    table = locator.get('table')
    columns = locator.get('columns')
    if isinstance(table, str) and isinstance(columns, list):
        tokens.update(
            f'{table}.{column}' for column in columns if isinstance(column, str)
        )
    for row_key, row_value in _leaves(payload(item.result)):
        if row_key in _ROW_KEYS and isinstance(row_value, str) and row_value:
            tokens.add(row_value)
    return tokens
