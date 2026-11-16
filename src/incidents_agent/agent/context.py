"""The conversation, with older data-tool results replaced by digests."""

from collections.abc import Mapping

from incidents_agent.agent import config
from incidents_agent.agent import executor
from incidents_agent.agent import results
from incidents_agent.agent import trajectory
from incidents_agent.llm import contracts


class Conversation:
    """Message history that applies the context policy when it is sent.

    The last `keep_recent_results` data-tool results stay in full. Once
    `digest_block` more have accumulated, the oldest block is replaced by
    digests in one step, so the sent prefix stays identical between
    collapses and prompt caching keeps working. Full results stay in the
    result store and the trajectory.
    """

    def __init__(
        self, policy: config.ContextPolicy, store: results.ResultStore
    ) -> None:
        """Start an empty conversation over a run's result store."""
        self.policy = policy
        self.store = store
        self.messages: list[contracts.Message] = []
        self._data_calls: dict[int, str] = {}
        self.digested: set[str] = set()

    def append(
        self, message: contracts.Message, *, data_call_id: str | None = None
    ) -> None:
        """Add a message; `data_call_id` marks a data-tool result."""
        if data_call_id is not None:
            self._data_calls[len(self.messages)] = data_call_id
        self.messages.append(message)

    def _collapse(self) -> None:
        keep = self.policy.keep_recent_results
        block = self.policy.digest_block
        full = [
            call_id
            for _, call_id in sorted(self._data_calls.items())
            if call_id not in self.digested
        ]
        while len(full) >= keep + block:
            self.digested.update(full[:block])
            full = full[block:]

    def view(self) -> tuple[contracts.Message, ...]:
        """Return the messages to send, with older results digested."""
        self._collapse()
        sent: list[contracts.Message] = []
        for index, message in enumerate(self.messages):
            call_id = self._data_calls.get(index)
            stored = None if call_id is None else self.store.get(call_id)
            if call_id in self.digested and stored is not None:
                sent.append(
                    contracts.Message(
                        'tool',
                        executor.digest(stored, self.policy),
                        tool_call_id=message.tool_call_id,
                    )
                )
            else:
                sent.append(message)
        return tuple(sent)

    def to_dict(self) -> dict[str, object]:
        """Return the conversation for checkpoints."""
        return {
            'messages': [
                trajectory.message_to_dict(message) for message in self.messages
            ],
            'data_calls': {
                str(index): call_id
                for index, call_id in self._data_calls.items()
            },
            'digested': sorted(self.digested),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object],
        policy: config.ContextPolicy,
        store: results.ResultStore,
    ) -> 'Conversation':
        """Rebuild a conversation written by `to_dict`."""
        conversation = cls(policy, store)
        messages = data.get('messages')
        for item in messages if isinstance(messages, list) else []:
            if isinstance(item, dict):
                conversation.messages.append(trajectory.message_from_dict(item))
        calls = data.get('data_calls')
        if isinstance(calls, dict):
            conversation._data_calls = {
                int(index): str(call_id) for index, call_id in calls.items()
            }
        digested = data.get('digested')
        if isinstance(digested, list):
            conversation.digested = {str(item) for item in digested}
        return conversation
