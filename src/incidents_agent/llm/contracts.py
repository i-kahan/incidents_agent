"""Provider independent inputs, results, and failure types."""

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Literal
from typing import Protocol

type JSON = str | int | float | bool | None | list[JSON] | dict[str, JSON]


class ModelUnavailable(ValueError):
    """The selected model cannot safely be called."""


class BudgetRefused(RuntimeError):
    """The ledger refused to admit a paid attempt."""


class BillingUncertain(RuntimeError):
    """An attempt is still held pending billing reconciliation."""


class ProviderError(RuntimeError):
    """The provider rejected the request without a retryable error."""


class RetryDeferred(RuntimeError):
    """The caller can reschedule a transiently unavailable model."""

    def __init__(self, retry_after: float):
        """Report the delay before the caller may retry."""
        super().__init__(f'Model call deferred for {retry_after:g} seconds')
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Tool:
    """A client-side function the model may request."""

    name: str
    description: str
    parameters: Mapping[str, JSON]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model-requested function and its original JSON arguments."""

    id: str
    name: str
    arguments: str
    provider_fields: Mapping[str, JSON] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """A user or assistant turn, or the result of a tool call."""

    role: Literal['user', 'assistant', 'tool']
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    provider_fields: Mapping[str, JSON] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A text completion and its stable prompt prefix."""

    model_alias: str
    system_prompt: str
    max_output_tokens: int
    tools: tuple[Tool, ...] = ()
    catalog_briefing: str = ''
    history: tuple[Message, ...] = ()


@dataclass(frozen=True, slots=True)
class Usage:
    """Input and output totals, with cache and reasoning subsets."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        """Check that the usage subsets fit their totals."""
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError('Usage counts must be nonnegative integers')
        if self.cache_read_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError('Cache usage exceeds total input tokens')
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError('Reasoning usage exceeds total output tokens')


@dataclass(frozen=True, slots=True)
class Run:
    """Identity shared across workers for one run and its optional reserve."""

    id: str
    reserve_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Normalized response as seen by the spend-controlled client."""

    message: Message
    usage: Usage
    served_model: str
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """A successful call and the metadata needed to record it."""

    response: ProviderResult
    reservation_id: str
    started_at: str
    requested_model: str
    cost_usd: Decimal
    limit_usd: Decimal


class ModelClient(Protocol):
    """Interface shared by the guarded client and the scripted fake."""

    def complete(
        self, request: ChatRequest, *, run: Run, case_id: str | None = None
    ) -> ChatResult:
        """Return one assistant turn or raise a typed failure."""
