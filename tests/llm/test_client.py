"""Exercise guarded completions with scripted transport outcomes."""

import logging
import threading
from collections import deque
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from email.utils import format_datetime

import pytest

from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import fake
from incidents_agent.llm import ledger
from incidents_agent.llm import pricing


def _response(
    name: str = 'fake-20250101',
    usage: contracts.Usage = contracts.Usage(3, 2),
) -> contracts.ProviderResult:
    return contracts.ProviderResult(
        contracts.Message('assistant', 'Done'), usage, name, 'request-123'
    )


class _Script:
    def __init__(
        self,
        outputs: Sequence[contracts.ProviderResult | client.TransportFailure],
        bound: int | None = None,
    ) -> None:
        self.results = deque(outputs)
        self.bound = bound
        self.calls = 0

    def input_bound(
        self, spec: config.ModelSpec, request: contracts.ChatRequest
    ) -> int | None:
        return self.bound

    def complete(
        self,
        spec: config.ModelSpec,
        request: contracts.ChatRequest,
        *,
        api_key: str,
        cache: bool,
        timeout: float,
    ) -> contracts.ProviderResult:
        self.calls += 1
        result = self.results.popleft()
        if isinstance(result, client.TransportFailure):
            raise result
        return result


@pytest.fixture
def make_client(
    models: config.Models,
    spend: ledger.SpendLedger,
    rate_card: dict[str, object],
) -> Callable[..., client.GuardedClient]:
    """Build a guarded client with a predictable clock and price card."""

    def build(
        transport: client.Transport,
        *,
        sleep: Callable[[float], None] = lambda _: None,
        time: Callable[[], float] = lambda: 0,
        policy: client.RetryPolicy = client.RetryPolicy(),
    ) -> client.GuardedClient:
        return client.GuardedClient(
            models,
            {'ANTHROPIC_API_KEY': 'fake-key'},
            spend,
            transport,
            catalog=rate_card,
            sleep=sleep,
            monotonic=time,
            wall_time=lambda: 0,
            jitter=lambda: 0,
            policy=policy,
        )

    return build


def test_unknown_model_is_reported(
    make_client: Callable[..., client.GuardedClient],
) -> None:
    """Return a typed error for an unknown alias."""
    llm = make_client(_Script([]))

    with pytest.raises(contracts.ModelUnavailable, match='No configured'):
        llm.model_info('absent')


def test_missing_key_leaves_configured_default_unavailable(
    models: config.Models,
    spend: ledger.SpendLedger,
    rate_card: dict[str, object],
    request_data: contracts.ChatRequest,
) -> None:
    """Skip the default when its provider lacks credentials."""
    transport = _Script([])
    llm = client.GuardedClient(models, {}, spend, transport, catalog=rate_card)

    with pytest.raises(contracts.ModelUnavailable, match='missing ANTHROPIC'):
        llm.complete(request_data, run=contracts.Run('demo'))

    assert transport.calls == 0


def test_unpriced_model_never_dispatches(
    models: config.Models,
    spend: ledger.SpendLedger,
    request_data: contracts.ChatRequest,
) -> None:
    """Require pricing before reaching the provider transport."""
    transport = _Script([])
    llm = client.GuardedClient(
        models, {'ANTHROPIC_API_KEY': 'fake-key'}, spend, transport, catalog={}
    )

    with pytest.raises(
        contracts.ModelUnavailable, match='Missing LiteLLM rate'
    ):
        llm.complete(request_data, run=contracts.Run('demo'))

    assert transport.calls == 0


def test_price_override_can_supply_rates_for_new_model() -> None:
    """Apply owner rates for an otherwise unpriced model."""
    spec = config.ModelSpec(
        'new',
        'openai/new-2025-01-01',
        'openai',
        config.PriceOverride(Decimal(2), Decimal(4), Decimal('0.5')),
        100,
    )

    price = pricing.resolve(spec, {})

    assert price.cost(contracts.Usage(10, 5, cache_read_tokens=4)) == Decimal(
        '0.000034'
    )


def test_price_without_a_hard_ceiling_is_refused() -> None:
    """Require an input limit even when model prices are overridden."""
    spec = config.ModelSpec(
        'new',
        'openai/new',
        'openai',
        config.PriceOverride(Decimal(1), Decimal(2)),
    )
    with pytest.raises(contracts.ModelUnavailable, match='hard input'):
        pricing.resolve(spec, {})


def test_long_context_tier_affects_bound_and_settlement() -> None:
    """Apply the appropriate context tier to bounds and usage."""
    model = config.ModelSpec('wide', 'openai/wide-2025-01-01', 'openai')
    price = pricing.resolve(
        model,
        {
            model.id: {
                'litellm_provider': 'openai',
                'max_input_tokens': 100,
                'input_cost_per_token': '0.01',
                'output_cost_per_token': '0.02',
                'input_cost_per_token_above_50_tokens': '0.03',
                'output_cost_per_token_above_50_tokens': '0.04',
            }
        },
    )

    assert price.bound(100, 2) == Decimal('3.08')
    assert price.cost(contracts.Usage(51, 2)) == Decimal('1.61')
    assert price.cost(contracts.Usage(50, 2)) == Decimal('0.54')


def test_cached_reads_and_writes_settle_at_their_own_rates(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
) -> None:
    """Price all token categories and record the settled call."""
    usage = contracts.Usage(40, 5, cache_read_tokens=10, cache_write_tokens=20)
    llm = make_client(_Script([_response(usage=usage)]))
    run = contracts.Run('demo')

    result = llm.complete(request_data, run=run)

    assert result.cost_usd == Decimal('0.046')
    assert result.limit_usd == Decimal('1.75')
    assert spend.counters(run) == ledger.RunCounters(
        1, 1, usage, result.cost_usd, Decimal(0)
    )
    assert spend.attempt_records(run)[0]['request_id'] == 'request-123'


def test_drift_uses_served_price_and_records_warning(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    caplog: pytest.LogCaptureFixture,
    spend: ledger.SpendLedger,
) -> None:
    """Record and price the model that actually served the response."""
    llm = make_client(_Script([_response('fake-20250202')]))
    run = contracts.Run('demo')

    with caplog.at_level(logging.WARNING):
        result = llm.complete(request_data, run=run)

    assert result.cost_usd == Decimal('0.009')
    assert spend.attempt_records(run)[0]['served_model'] == 'fake-20250202'
    assert 'provider served fake-20250202' in caplog.text


def test_unknown_served_price_holds_the_original_reservation(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
) -> None:
    """Retain the worst-case hold for unpriceable served IDs."""
    llm = make_client(_Script([_response('unpriced')]))
    run = contracts.Run('demo')

    with pytest.raises(contracts.BillingUncertain, match='Unknown price'):
        llm.complete(request_data, run=run)

    assert spend.attempt_records(run)[0]['served_model'] == 'unpriced'
    assert spend.snapshot().held == Decimal('0.145')


def test_overrun_is_reported_as_uncertain(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
) -> None:
    """Surface excessive billed usage and halt new admissions."""
    llm = make_client(_Script([_response(usage=contracts.Usage(1000, 0))]))

    with pytest.raises(contracts.BillingUncertain, match='exceeded the bound'):
        llm.complete(request_data, run=contracts.Run('demo'))

    assert spend.snapshot().halted


@pytest.mark.parametrize(
    'header',
    ['3', format_datetime(datetime.fromtimestamp(3, UTC), usegmt=True)],
)
def test_retry_after_is_honored_and_each_attempt_has_its_own_hold(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
    header: str,
) -> None:
    """Honor both header forms and give retries their own records."""
    sleeps: list[float] = []
    llm = make_client(
        _Script(
            [
                client.TransportFailure(
                    'rate limited',
                    retryable=True,
                    unbilled=True,
                    retry_after=header,
                ),
                _response(),
            ]
        ),
        sleep=sleeps.append,
    )
    run = contracts.Run('demo')

    llm.complete(request_data, run=run)

    assert sleeps == [3]
    attempts = spend.attempt_records(run)
    assert [row['status'] for row in attempts] == ['unbilled', 'success']
    assert len({str(row['id']) for row in attempts}) == 2


def test_retry_after_outside_window_is_deferred(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
) -> None:
    """Return the requested delay if waiting exceeds the call window."""
    transport = _Script(
        [client.TransportFailure('429', retryable=True, retry_after='1000')]
    )
    llm = make_client(transport)

    with pytest.raises(contracts.RetryDeferred) as exc:
        llm.complete(request_data, run=contracts.Run('demo'))

    assert exc.value.retry_after == 1000
    assert transport.calls == 1


def test_timeout_hold_survives_a_successful_retry(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
) -> None:
    """Keep a timed-out attempt reserved after a successful retry."""
    llm = make_client(
        _Script(
            [client.TransportFailure('timeout', retryable=True), _response()]
        )
    )
    run = contracts.Run('demo')

    llm.complete(request_data, run=run)

    assert spend.snapshot().held == Decimal('0.145')
    assert spend.attempt_records(run)[0]['status'] == 'ambiguous'


def test_retry_is_refused_if_original_ambiguous_attempt_uses_balance(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
) -> None:
    """Check the same case cap again before retrying."""
    transport = _Script([client.TransportFailure('timeout', retryable=True)])
    llm = make_client(transport)
    run = contracts.Run('demo')
    spend.reserve(run, 'case', 'prior', Decimal('0.56'))

    with pytest.raises(contracts.BudgetRefused, match='Per-case'):
        llm.complete(request_data, run=run, case_id='case')

    assert transport.calls == 1


@pytest.mark.parametrize(
    ('unbilled', 'error'),
    [(False, contracts.BillingUncertain), (True, contracts.ProviderError)],
)
def test_nonretryable_failures_keep_only_unconfirmed_charges(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
    unbilled: bool,
    error: type[Exception],
) -> None:
    """Distinguish known rejections from uncertain charges."""
    llm = make_client(
        _Script(
            [
                client.TransportFailure('bad response', unbilled=unbilled),
            ]
        )
    )

    with pytest.raises(error):
        llm.complete(request_data, run=contracts.Run('demo'))

    assert spend.snapshot().held == (
        Decimal(0) if unbilled else Decimal('0.145')
    )


def test_transport_can_supply_a_tighter_verified_input_bound(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
) -> None:
    """Reserve from an independently verified request bound."""
    llm = make_client(_Script([_response()], bound=4))
    run = contracts.Run('demo')

    llm.complete(request_data, run=run)

    assert Decimal(str(spend.attempt_records(run)[0]['bound_usd'])) == (
        Decimal('0.025')
    )


def test_excessive_output_cap_is_detected_before_dispatch(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
) -> None:
    """Reject output caps that exceed the model limit."""
    llm = make_client(_Script([]))

    with pytest.raises(ValueError, match='output token cap'):
        llm.complete(
            replace(request_data, max_output_tokens=101),
            run=contracts.Run('demo'),
        )


class _BlockingTransport(_Script):
    def __init__(self) -> None:
        super().__init__([])
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()
        self.count_lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def complete(
        self, *args: object, **kwargs: object
    ) -> contracts.ProviderResult:
        with self.count_lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.entered.release()
        assert self.release.wait(timeout=5)
        with self.count_lock:
            self.active -= 1
        return _response()


def test_workers_share_the_provider_concurrency_cap(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
) -> None:
    """Keep multiple clients within the same provider cap."""
    transport = _BlockingTransport()
    waiting = threading.Event()
    resumed = threading.Event()

    def pause(_: float) -> None:
        waiting.set()
        assert resumed.wait(timeout=5)

    llm = make_client(transport)
    other = make_client(transport, sleep=pause)
    run = contracts.Run('parallel')
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(llm.complete, request_data, run=run)
        second = pool.submit(llm.complete, request_data, run=run)
        assert transport.entered.acquire(timeout=5)
        assert transport.entered.acquire(timeout=5)
        third = pool.submit(other.complete, request_data, run=run)
        try:
            assert waiting.wait(timeout=5)
            assert transport.peak == 2
        finally:
            transport.release.set()
        first.result(timeout=5)
        second.result(timeout=5)
        resumed.set()
        third.result(timeout=5)

    assert transport.peak == 2


def test_scripted_model_returns_tool_calls_in_order(
    request_data: contracts.ChatRequest,
) -> None:
    """Return tool calls and failures in the queued order."""
    tool = contracts.ToolCall('tool-1', 'read_data', '{}')
    message = contracts.Message('assistant', tool_calls=(tool,))
    demo = contracts.Run('demo')
    scripted = fake.ScriptedClient([message, ValueError('scripted error')])

    result = scripted.complete(request_data, run=demo, case_id='case')

    assert result.response.message.tool_calls == (tool,)
    assert scripted.requests == [(request_data, demo, 'case')]
    with pytest.raises(ValueError, match='scripted error'):
        scripted.complete(request_data, run=demo)
    with pytest.raises(AssertionError, match='no response'):
        scripted.complete(request_data, run=demo)


_WRITE_RATE = Decimal('0.00000125')
_OUTPUT_RATE = Decimal('0.000002')


def _use_wide_cheap_model(rate_card: dict[str, object]) -> None:
    """Give the default model a 1M-token window at per-million-like rates."""
    record = rate_card['fake-20250101']
    assert isinstance(record, dict)
    record.update(
        {
            'max_input_tokens': 1_000_000,
            'input_cost_per_token': '0.000001',
            'output_cost_per_token': str(_OUTPUT_RATE),
            'cache_read_input_token_cost': '0.0000001',
            'cache_creation_input_token_cost': str(_WRITE_RATE),
        }
    )


def test_request_input_bound_counts_utf8_bytes_of_every_part() -> None:
    """Every part of the request adds at least its UTF-8 size."""
    empty = contracts.ChatRequest('', '', 10)
    base = client.request_input_bound(empty)
    accented = contracts.ChatRequest('', 'é' * 100, 10)
    with_tool = replace(
        empty, tools=(contracts.Tool('t', 'd' * 300, {'type': 'object'}),)
    )
    call = contracts.ToolCall('c1', 'run_sql', '{"query": "' + 'q' * 400)
    with_turns = replace(
        empty,
        history=(
            contracts.Message('assistant', None, (call,)),
            contracts.Message('tool', 'r' * 500, tool_call_id='c1'),
            contracts.Message('user', None, provider_fields={'sig': 's' * 600}),
        ),
    )

    assert base >= client.FIXED_OVERHEAD_TOKENS
    assert client.request_input_bound(accented) >= base + 200
    assert client.request_input_bound(with_tool) >= base + 300
    assert client.request_input_bound(with_turns) >= base + 1_500


def test_reservation_uses_request_size_not_context_window(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    rate_card: dict[str, object],
    spend: ledger.SpendLedger,
) -> None:
    """A short request on a 1M-token model reserves for its own size."""
    _use_wide_cheap_model(rate_card)
    llm = make_client(_Script([_response()]))
    run = contracts.Run('demo')

    llm.complete(request_data, run=run)

    tokens = client.request_input_bound(request_data)
    expected = Decimal(tokens) * _WRITE_RATE + 10 * _OUTPUT_RATE
    bound = Decimal(str(spend.attempt_records(run)[0]['bound_usd']))
    assert tokens < 10_000
    assert bound == expected
    assert bound < Decimal(1_000_000) * _WRITE_RATE


def test_ambiguous_attempt_holds_only_the_request_bound(
    make_client: Callable[..., client.GuardedClient],
    request_data: contracts.ChatRequest,
    rate_card: dict[str, object],
    spend: ledger.SpendLedger,
) -> None:
    """A timeout keeps the request-sized bound held, not the full window."""
    _use_wide_cheap_model(rate_card)
    failure = client.TransportFailure('timed out', retryable=False)
    llm = make_client(_Script([failure]))

    with pytest.raises(contracts.BillingUncertain):
        llm.complete(request_data, run=contracts.Run('demo'))

    tokens = client.request_input_bound(request_data)
    assert spend.snapshot().held == (
        Decimal(tokens) * _WRITE_RATE + 10 * _OUTPUT_RATE
    )
