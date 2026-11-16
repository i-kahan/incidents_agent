"""Explicit evaluation retries and a disabled-by-default routing seam."""

import dataclasses
import math
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Protocol

from incidents_agent.llm import contracts

POLICY = {'version': 1, 'attempts': 2, 'window_s': 90, 'fallback': None}


@dataclasses.dataclass(frozen=True)
class Failure:
    """Sanitized terminal or retryable failure with billing precedence."""

    category: str
    error: str
    retryable: bool = False
    delay_s: float = 0
    billing: str = 'unknown'

    def to_dict(self) -> dict[str, object]:
        """Return durable failure metadata."""
        return dataclasses.asdict(self)


class AttemptFailure(Exception):
    """An executor's classified failure, safe for the runner to journal."""

    def __init__(self, failure: Failure) -> None:
        """Carry classification without losing the original error type."""
        super().__init__(failure.error)
        self.failure = failure


class ModelStrategy(Protocol):
    """Extension point; future fallback policies must be fingerprinted."""

    def next_model(
        self,
        primary: str,
        failure: Failure,
        history: Sequence[Mapping[str, object]],
    ) -> str | None:
        """Choose an eligible next attempt, or stop."""


class PrimaryOnly:
    """Retry the primary model once; never silently route to another."""

    def next_model(
        self,
        primary: str,
        failure: Failure,
        history: Sequence[Mapping[str, object]],
    ) -> str | None:
        """Return the primary only when the retry budget permits it."""
        if failure.retryable and len(history) < 2:
            return primary
        return None


def classify(
    error: Exception, *, held: bool = False, settled: bool = False
) -> Failure:
    """Classify known errors; callers must not hide programming failures."""
    name = type(error).__name__
    if held or isinstance(error, contracts.BillingUncertain):
        return Failure('billing_reconciliation_required', name)
    if isinstance(error, contracts.BudgetRefused):
        return Failure('budget_refused', name, billing='unbilled')
    if isinstance(error, contracts.RetryDeferred):
        finite = math.isfinite(error.retry_after)
        delay = max(0.0, error.retry_after) if finite else 0.0
        return Failure(
            'provider_deferred',
            name,
            finite and delay <= 90,
            delay,
            'unbilled',
        )
    if isinstance(error, (contracts.ProviderError, contracts.ModelUnavailable)):
        return Failure('provider_permanent', name, billing='unbilled')
    if isinstance(error, (ValueError, FileNotFoundError)):
        return Failure(
            'invalid_response' if settled else 'invalid_input',
            name,
            settled,
            billing='settled' if settled else 'unbilled',
        )
    raise error
