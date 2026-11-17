"""Offline HTTP contract tests for the locked LiteLLM provider adapters."""

import json
from decimal import Decimal
from typing import Literal

import httpx
import httpx2
import litellm
import openai
import pytest

from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger
from incidents_agent.llm import litellm_transport

OPENAI_MODEL = 'gpt-4.1-mini-2025-04-14'
ANTHROPIC_MODEL = 'claude-haiku-4-5-20251001'
GEMINI_MODEL = 'gemini-2.5-flash'


def _openai_payload() -> dict[str, object]:
    return {
        'id': 'chatcmpl-1',
        'object': 'chat.completion',
        'created': 42,
        'model': OPENAI_MODEL,
        'choices': [
            {
                'index': 0,
                'finish_reason': 'tool_calls',
                'message': {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [
                        {
                            'id': 'call_1',
                            'type': 'function',
                            'function': {
                                'name': 'read_data',
                                'arguments': '{}',
                            },
                        }
                    ],
                },
            }
        ],
        'usage': {
            'prompt_tokens': 20,
            'completion_tokens': 3,
            'total_tokens': 23,
            'prompt_tokens_details': {'cached_tokens': 4},
        },
    }


def _anthropic_payload() -> dict[str, object]:
    return {
        'id': 'msg_1',
        'type': 'message',
        'role': 'assistant',
        'model': ANTHROPIC_MODEL,
        'content': [
            {
                'type': 'tool_use',
                'id': 'toolu_1',
                'name': 'read_data',
                'input': {},
            }
        ],
        'stop_reason': 'tool_use',
        'stop_sequence': None,
        'usage': {
            'input_tokens': 9,
            'output_tokens': 3,
            'cache_read_input_tokens': 4,
            'cache_creation_input_tokens': 7,
        },
    }


def _gemini_payload(finish: str = 'STOP') -> dict[str, object]:
    return {
        'modelVersion': GEMINI_MODEL,
        'responseId': 'gemini-1',
        'candidates': [
            {
                'index': 0,
                'finishReason': finish,
                'continuationToken': 'page-2',
                'content': {
                    'role': 'model',
                    'parts': [
                        {'functionCall': {'name': 'read_data', 'args': {}}},
                    ],
                },
            }
        ],
        'usageMetadata': {
            'promptTokenCount': 20,
            'candidatesTokenCount': 2,
            'thoughtsTokenCount': 1,
            'totalTokenCount': 23,
            'cachedContentTokenCount': 4,
        },
    }


@pytest.fixture(autouse=True)
def offline_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use bundled model data and mock requests during adapter tests."""
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    monkeypatch.setenv('LITELLM_OFFLINE_MODE', 'True')
    monkeypatch.setenv('LITELLM_TELEMETRY', 'False')


@pytest.mark.integration
@pytest.mark.parametrize(
    ('provider', 'model', 'expected_usage'),
    [
        ('openai', OPENAI_MODEL, contracts.Usage(20, 3, 4)),
        ('anthropic', ANTHROPIC_MODEL, contracts.Usage(20, 3, 4, 7)),
        ('gemini', GEMINI_MODEL, contracts.Usage(20, 3, 4, 0, 1)),
    ],
)
def test_provider_wire_request_keeps_cap_prefix_and_tool_schema(
    request_data: contracts.ChatRequest,
    provider: str,
    model: str,
    expected_usage: contracts.Usage,
) -> None:
    """Exercise each provider translator with offline HTTP."""
    seen: list[dict[str, object]] = []

    def openai(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200, json=_openai_payload(), headers={'x-request-id': 'r1'}
        )

    def other(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        payload = (
            _anthropic_payload()
            if provider == 'anthropic'
            else _gemini_payload()
        )
        return httpx.Response(200, json=payload, headers={'request-id': 'r1'})

    transport = litellm_transport.LiteLLMTransport(
        httpx.MockTransport(other),
        openai_transport=httpx2.MockTransport(openai),
    )

    result = transport.complete(
        config.ModelSpec('demo', f'{provider}/{model}', provider),
        request_data,
        api_key='mock',
        cache=True,
        timeout=2,
    )

    assert result.served_model == model
    assert result.request_id == 'r1'
    assert result.usage == expected_usage
    assert result.message.tool_calls[0].name == 'read_data'
    (body,) = seen
    if provider == 'gemini':
        generation = config.mapping(body['generationConfig'], 'generation')
        assert generation['temperature'] == 0
        cap = generation.get(
            'maxOutputTokens', generation.get('max_output_tokens')
        )
        assert cap == request_data.max_output_tokens
    else:
        assert body['temperature'] == 0
        assert body.get('max_completion_tokens', body.get('max_tokens')) == (
            request_data.max_output_tokens
        )
        if provider == 'openai':
            assert body['service_tier'] == 'default'
    serialized = json.dumps(body)
    assert '"no_log"' not in serialized
    assert '"no-log"' not in serialized
    assert serialized.index('Catalog') < serialized.index('Start')
    assert 'read_data' in serialized
    assert ('cache_control' in serialized) == (provider == 'anthropic')


@pytest.mark.integration
def test_gemini_sdk_cannot_dispatch_a_second_request(
    request_data: contracts.ChatRequest,
) -> None:
    """Block an SDK continuation within the original reservation."""
    received: list[str] = []

    def other(request: httpx.Request) -> httpx.Response:
        received.append(request.url.path)
        return httpx.Response(200, json=_gemini_payload('CONTINUATION'))

    transport = litellm_transport.LiteLLMTransport(httpx.MockTransport(other))
    spec = config.ModelSpec('demo', f'gemini/{GEMINI_MODEL}', 'gemini')

    with pytest.raises(
        client.TransportFailure, match='second provider dispatch'
    ):
        transport.complete(
            spec, request_data, api_key='mock', cache=True, timeout=2
        )

    assert len(received) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ('status', 'outcome'),
    [(429, 'unbilled'), (503, 'ambiguous')],
)
def test_provider_status_preserves_retry_after_and_billing(
    request_data: contracts.ChatRequest,
    status: int,
    outcome: Literal['unbilled', 'ambiguous'],
    spend: ledger.SpendLedger,
) -> None:
    """Classify throttling and ambiguous provider failures."""

    def openai(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status,
            json={'error': {'message': 'should not appear in logs'}},
            headers={'Retry-After': '60', 'x-request-id': 'r2'},
        )

    transport = litellm_transport.LiteLLMTransport(
        openai_transport=httpx2.MockTransport(openai)
    )
    spec = config.ModelSpec('demo', f'openai/{OPENAI_MODEL}', 'openai')
    llm = client.GuardedClient(
        config.Models({'demo': spec}, 'demo'),
        {'OPENAI_API_KEY': 'mock'},
        spend,
        transport,
        policy=client.RetryPolicy(attempts=1),
    )
    run = contracts.Run('error')

    with pytest.raises(contracts.RetryDeferred) as error:
        llm.complete(request_data, run=run)

    assert error.value.retry_after == 60
    (record,) = spend.attempt_records(run)
    assert record['status'] == outcome
    assert record['request_id'] == 'r2'
    assert record['reason'] == f'Provider HTTP {status}'
    assert (spend.snapshot().held > Decimal(0)) == (outcome == 'ambiguous')


@pytest.mark.integration
def test_unsent_connection_error_is_unbilled_and_retryable(
    request_data: contracts.ChatRequest,
    spend: ledger.SpendLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure before any dispatch cannot have billed, so it may retry."""

    def refuse(**kwargs: object) -> None:
        del kwargs
        raise openai.APIConnectionError(
            request=httpx2.Request('POST', 'https://api.invalid')
        )

    monkeypatch.setattr(litellm, 'completion', refuse)
    transport = litellm_transport.LiteLLMTransport()
    spec = config.ModelSpec('demo', f'openai/{OPENAI_MODEL}', 'openai')
    llm = client.GuardedClient(
        config.Models({'demo': spec}, 'demo'),
        {'OPENAI_API_KEY': 'mock'},
        spend,
        transport,
        policy=client.RetryPolicy(attempts=1),
    )
    run = contracts.Run('refused')

    with pytest.raises(contracts.RetryDeferred):
        llm.complete(request_data, run=run)

    (record,) = spend.attempt_records(run)
    assert record['status'] == 'unbilled'
    assert spend.snapshot().held == Decimal(0)
