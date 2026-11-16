"""Scripted chat client for agent and classifier tests."""

from collections import deque
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from threading import Lock

from incidents_agent.llm import contracts


class ScriptedClient:
    """Return queued text and tool calls or raise queued failures."""

    def __init__(
        self,
        responses: Iterable[
            contracts.Message | contracts.ChatResult | Exception
        ],
    ) -> None:
        """Load the scripted turns and failures in call order."""
        self._responses = deque(responses)
        self._lock = Lock()
        self.requests: list[
            tuple[contracts.ChatRequest, contracts.Run, str | None]
        ] = []

    def complete(
        self,
        request: contracts.ChatRequest,
        *,
        run: contracts.Run,
        case_id: str | None = None,
    ) -> contracts.ChatResult:
        """Record a request and consume exactly one scripted response."""
        with self._lock:
            self.requests.append((request, run, case_id))
            if not self._responses:
                raise AssertionError('Scripted model has no response remaining')
            response = self._responses.popleft()
            call_id = f'fake-{len(self.requests)}'
        if isinstance(response, Exception):
            raise response
        if isinstance(response, contracts.ChatResult):
            return response
        return contracts.ChatResult(
            contracts.ProviderResult(
                response,
                contracts.Usage(0, 0),
                request.model_alias,
                call_id,
            ),
            call_id,
            datetime(1970, 1, 1, tzinfo=UTC).isoformat(),
            request.model_alias,
            Decimal(0),
            Decimal(0),
        )
