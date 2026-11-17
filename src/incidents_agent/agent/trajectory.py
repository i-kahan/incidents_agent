"""The run record: an append-only JSON-lines trajectory of every event."""

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from incidents_agent.llm import contracts

TRAJECTORY_VERSION = 1

# Role tags written on events. `agent` is the single agent; the others are
# the Phase 07 multi-agent roles. Tester events also carry `hypothesis_id`.
ROLES: tuple[str, ...] = ('agent', 'lead', 'tester', 'skeptic')

type EventType = Literal[
    'run_start',
    'node',
    'model_call',
    'model_error',
    'tool_call',
    'agent_tool',
    'ledger',
    'verification',
    'submit_rejected',
    'final_report',
    'continuation',
]


class Trajectory:
    """Collect events in memory and append each one to a JSONL file.

    Every event carries the run id, the continuation index and a role tag,
    so multi-agent roles (Phase 07) share the same schema. Recording is
    safe from several threads: `seq` is assigned and the line appended
    under one lock, so sequence numbers have no gaps or repeats.
    """

    def __init__(
        self, path: Path | None, run_id: str, continuation: int = 0
    ) -> None:
        """Open the record; `path` None keeps events in memory only."""
        self.path = path
        self.run_id = run_id
        self.continuation = continuation
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                self.events = read_trajectory(path)

    def record(
        self,
        event_type: EventType,
        role: str = 'agent',
        **fields: object,
    ) -> dict[str, object]:
        """Append one event and return it.

        Args:
            event_type: Kind of event.
            role: Role that produced it, one of `ROLES`.
            **fields: JSON-serialisable event body. A `hypothesis_id` of
                None (an event not tied to a tester) is left out.

        Returns:
            The event as written.
        """
        if 'hypothesis_id' in fields and fields['hypothesis_id'] is None:
            del fields['hypothesis_id']
        with self._lock:
            event: dict[str, object] = {
                'v': TRAJECTORY_VERSION,
                'seq': len(self.events) + 1,
                'type': event_type,
                'run_id': self.run_id,
                'continuation': self.continuation,
                'role': role,
                **fields,
            }
            line = json.dumps(event, default=str, ensure_ascii=False)
            written: dict[str, object] = json.loads(line)
            self.events.append(written)
            if self.path is not None:
                with self.path.open('a', encoding='utf-8') as output:
                    output.write(line + '\n')
        return written

    def of_type(self, event_type: EventType) -> list[dict[str, object]]:
        """Return the events of one type, in order."""
        with self._lock:
            events = list(self.events)
        return [event for event in events if event['type'] == event_type]


def read_trajectory(path: Path) -> list[dict[str, object]]:
    """Read every event from a trajectory file.

    Args:
        path: A `trajectory.jsonl` written by `Trajectory`.

    Returns:
        The events in order.

    Raises:
        ValueError: A line is not a JSON object.
    """
    events: list[dict[str, object]] = []
    for number, line in enumerate(
        path.read_text(encoding='utf-8').splitlines(), 1
    ):
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError(f'{path}:{number} is not a JSON object')
        events.append(event)
    return events


_ROLES: dict[str, Literal['user', 'assistant', 'tool']] = {
    'user': 'user',
    'assistant': 'assistant',
    'tool': 'tool',
}


def message_to_dict(message: contracts.Message) -> dict[str, object]:
    """Return a JSON-ready form of a conversation message."""
    return {
        'role': message.role,
        'text': message.text,
        'tool_call_id': message.tool_call_id,
        'tool_calls': [
            {
                'id': call.id,
                'name': call.name,
                'arguments': call.arguments,
                'provider_fields': dict(call.provider_fields),
            }
            for call in message.tool_calls
        ],
        'provider_fields': dict(message.provider_fields),
    }


def message_from_dict(data: Mapping[str, object]) -> contracts.Message:
    """Rebuild a message written by `message_to_dict`."""
    role = _ROLES.get(str(data['role']))
    if role is None:
        raise ValueError(f'Unknown message role {data["role"]!r}')
    raw_calls = data.get('tool_calls') or []
    if not isinstance(raw_calls, list):
        raise ValueError('tool_calls must be a list')
    calls = tuple(
        contracts.ToolCall(
            str(call['id']),
            str(call['name']),
            str(call['arguments']),
            _json_mapping(call.get('provider_fields')),
        )
        for call in raw_calls
        if isinstance(call, dict)
    )
    text = data.get('text')
    call_id = data.get('tool_call_id')
    return contracts.Message(
        role,
        None if text is None else str(text),
        calls,
        None if call_id is None else str(call_id),
        _json_mapping(data.get('provider_fields')),
    )


def _json_mapping(value: object) -> dict[str, contracts.JSON]:
    if not isinstance(value, dict):
        return {}
    loaded: dict[str, contracts.JSON] = json.loads(json.dumps(value))
    return loaded
