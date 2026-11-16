"""Model call orchestration, retries, and reservation settlement."""

import fcntl
import logging
import math
import random
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Protocol

from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger
from incidents_agent.llm import pricing

LOGGER = logging.getLogger(__name__)


class TransportFailure(Exception):
    """A sanitized description of one attempted provider request."""

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool = False,
        unbilled: bool = False,
        retry_after: str | None = None,
        request_id: str | None = None,
        served_model: str | None = None,
    ) -> None:
        """Record the outcome information available after dispatch."""
        super().__init__(reason)
        self.retryable = retryable
        self.unbilled = unbilled
        self.retry_after = retry_after
        self.request_id = request_id
        self.served_model = served_model


class Transport(Protocol):
    """Sends one physical attempt per invocation."""

    def complete(
        self,
        spec: config.ModelSpec,
        request: contracts.ChatRequest,
        *,
        api_key: str,
        cache: bool,
        timeout: float,
    ) -> contracts.ProviderResult:
        """Send a completion, or raise TransportFailure with billing status."""

    def input_bound(
        self, spec: config.ModelSpec, request: contracts.ChatRequest
    ) -> int | None:
        """Return a proven upper bound or defer to the model's hard ceiling."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Per-provider concurrency and timing defaults for one process group."""

    concurrency: int = 2
    attempts: int = 3
    timeout: float = 30
    retry_window: float = 90

    def __post_init__(self) -> None:
        """Reject policies that could never admit a request."""
        if (
            self.concurrency < 1
            or self.attempts < 1
            or not all(
                math.isfinite(n) and n > 0
                for n in (self.timeout, self.retry_window)
            )
        ):
            raise ValueError('Invalid request retry policy')


def _retry_delay(value: str | None, now: float) -> float:
    if not value:
        return 0
    candidate = value.strip()
    if candidate.isascii() and candidate.isdecimal():
        try:
            return float(candidate)
        except OverflowError:
            return math.inf
    try:
        date = parsedate_to_datetime(candidate)
        return max(0, date.replace(tzinfo=date.tzinfo or UTC).timestamp() - now)
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Safe diagnostic information for one configured model."""

    spec: config.ModelSpec
    has_key: bool
    price: pricing.Price | None
    issues: tuple[str, ...]

    @property
    def usable(self) -> bool:
        """Whether local checks permit calls to this alias."""
        return not self.issues


def model_info(
    models: config.Models,
    env: Mapping[str, str],
    catalog: Mapping[str, object],
    alias: str,
) -> ModelInfo:
    """Describe local availability without calling a provider."""
    spec = models.aliases.get(alias)
    if spec is None:
        raise contracts.ModelUnavailable(f'No configured model alias {alias!r}')
    key_name = config.PROVIDER_KEYS[spec.provider]
    has_key = bool(env.get(key_name, '').strip())
    issues = [] if has_key else [f'missing {key_name}']
    price = None
    if spec.placeholder:
        issues.append('replace the model ID placeholder')
    else:
        try:
            price = pricing.resolve(spec, catalog)
        except contracts.ModelUnavailable as exc:
            issues.append(str(exc))
    return ModelInfo(spec, has_key, price, tuple(issues))


class GuardedClient:
    """Provider-independent chat client that reserves every attempt."""

    def __init__(
        self,
        models: config.Models,
        env: Mapping[str, str],
        spend: ledger.SpendLedger,
        transport: Transport,
        *,
        catalog: Mapping[str, object] | None = None,
        policy: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        """Use the shared ledger for every physical transport attempt."""
        self.models = models
        self.spend = spend
        self._env = env
        self._transport = transport
        self._catalog = (
            pricing.bundled_catalog() if catalog is None else catalog
        )
        self._policy = policy
        self._sleep = sleep
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._jitter = jitter

    def model_info(self, alias: str) -> ModelInfo:
        """Describe a model configured for this client."""
        return model_info(self.models, self._env, self._catalog, alias)

    @contextmanager
    def _slot(self, provider: str, deadline: float) -> Iterator[None]:
        # All workers sharing a ledger also share these OS locks. A process
        # crash releases the slot but leaves its spend reservation intact.
        while self._monotonic() < deadline:
            for slot in range(self._policy.concurrency):
                path = self.spend.path.with_name(
                    f'{self.spend.path.name}.{provider}.{slot}.lock'
                )
                with path.open('a+b') as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    yield
                    return
            self._sleep(max(0, min(0.05, deadline - self._monotonic())))
        raise contracts.RetryDeferred(0.05)

    def _finish(
        self,
        spec: config.ModelSpec,
        price: pricing.Price,
        reserved: ledger.Reservation,
        response: contracts.ProviderResult,
    ) -> contracts.ChatResult:
        served = response.served_model.removeprefix(f'{spec.provider}/')
        if served != spec.id.split('/', 1)[1]:
            LOGGER.warning(
                'Requested %s but provider served %s', spec.id, served
            )
            if spec.override is None:
                try:
                    price = pricing.resolve(
                        config.ModelSpec(
                            spec.alias,
                            f'{spec.provider}/{served}',
                            spec.provider,
                        ),
                        self._catalog,
                    )
                except contracts.ModelUnavailable as exc:
                    self.spend.finish(
                        reserved,
                        'ambiguous',
                        response=response,
                        reason='unknown served model price',
                    )
                    raise contracts.BillingUncertain(
                        f'Unknown price for served model on {reserved.id}'
                    ) from exc
        cost = price.cost(response.usage)
        if self.spend.finish(reserved, 'success', cost=cost, response=response):
            raise contracts.BillingUncertain(
                f'Reported cost exceeded the bound on {reserved.id}'
            )
        return contracts.ChatResult(
            response,
            reserved.id,
            reserved.started_at,
            spec.id,
            cost,
            reserved.limit,
        )

    def complete(
        self,
        request: contracts.ChatRequest,
        *,
        run: contracts.Run,
        case_id: str | None = None,
    ) -> contracts.ChatResult:
        """Send one guarded call with a reservation for each attempt."""
        info = self.model_info(request.model_alias or self.models.default)
        spec, price = info.spec, info.price
        if not info.usable or price is None:
            raise contracts.ModelUnavailable(
                f'{spec.alias}: {"; ".join(info.issues)}'
            )
        output = request.max_output_tokens
        if (
            type(output) is not int
            or not 0 < output <= 100_000_000
            or (
                price.output_ceiling is not None
                and output > price.output_ceiling
            )
        ):
            raise ValueError('Invalid output token cap for selected model')
        safe_input = self._transport.input_bound(spec, request)
        if safe_input is not None and (
            type(safe_input) is not int or safe_input < 0
        ):
            raise contracts.ModelUnavailable(
                'Transport returned an invalid bound'
            )
        input_bound = (
            price.input_ceiling
            if safe_input is None
            else min(safe_input, price.input_ceiling)
        )
        bound = price.bound(input_bound, output)
        key = self._env[config.PROVIDER_KEYS[spec.provider]].strip()
        deadline = self._monotonic() + self._policy.retry_window
        for index in range(self._policy.attempts):
            with self._slot(spec.provider, deadline):
                reserved = self.spend.reserve(run, case_id, spec.id, bound)
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    self.spend.finish(
                        reserved, 'unbilled', reason='retry window'
                    )
                    raise contracts.RetryDeferred(0)
                try:
                    response = self._transport.complete(
                        spec,
                        request,
                        api_key=key,
                        cache=price.cache_supported,
                        timeout=min(remaining, self._policy.timeout),
                    )
                except TransportFailure as failure:
                    self.spend.finish(
                        reserved,
                        'unbilled' if failure.unbilled else 'ambiguous',
                        request_id=failure.request_id,
                        reason=str(failure),
                        served_model=failure.served_model,
                    )
                    if not failure.retryable:
                        if failure.unbilled:
                            raise contracts.ProviderError(
                                str(failure)
                            ) from failure
                        raise contracts.BillingUncertain(
                            f'Billing is unknown for {reserved.id}'
                        ) from failure
                    delay = max(
                        _retry_delay(failure.retry_after, self._wall_time()),
                        min(8.0, 0.5 * 2 ** min(index, 4))
                        * (1 + self._jitter()),
                    )
                else:
                    return self._finish(spec, price, reserved, response)
            if index + 1 == self._policy.attempts or (
                self._monotonic() + delay >= deadline
            ):
                raise contracts.RetryDeferred(delay)
            self._sleep(delay)
        raise AssertionError('Retry policy must permit at least one attempt')
