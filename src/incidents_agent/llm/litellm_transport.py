"""Translate typed chat into one guarded LiteLLM provider request."""

import json
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import ExitStack
from typing import cast

import httpx
import httpx2

from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts


def _message(turn: contracts.Message) -> dict[str, object]:
    result: dict[str, object] = {'role': turn.role, 'content': turn.text}
    if turn.tool_call_id is not None:
        result['tool_call_id'] = turn.tool_call_id
    if turn.tool_calls:
        result['tool_calls'] = [
            {
                'id': call.id,
                'type': 'function',
                'function': {'name': call.name, 'arguments': call.arguments},
                **(
                    {'provider_specific_fields': dict(call.provider_fields)}
                    if call.provider_fields
                    else {}
                ),
            }
            for call in turn.tool_calls
        ]
    if turn.provider_fields:
        result['provider_specific_fields'] = dict(turn.provider_fields)
    return result


def _messages(
    request: contracts.ChatRequest, cache_mark: bool
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = [
        {'role': 'system', 'content': request.system_prompt}
    ]
    if request.catalog_briefing:
        messages.append({'role': 'user', 'content': request.catalog_briefing})
    if cache_mark and messages[-1]['content']:
        messages[-1]['content'] = [
            {
                'type': 'text',
                'text': messages[-1]['content'],
                'cache_control': {'type': 'ephemeral'},
            }
        ]
    messages.extend(_message(turn) for turn in request.history)
    return messages


def _str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f'Missing or invalid {key}')
    return value


def _optional_map(value: object) -> dict[str, object]:
    return {} if value is None else config.mapping(value, 'provider details')


def _tokens(data: Mapping[str, object], key: str, *, required: bool) -> int:
    value = data.get(key)
    if value is None and not required:
        return 0
    if type(value) is not int:
        raise ValueError(f'Missing or invalid {key}')
    return value


def _usage(response: Mapping[str, object]) -> contracts.Usage:
    usage = config.mapping(response.get('usage'), 'provider usage')
    details = _optional_map(usage.get('prompt_tokens_details'))
    completion = _optional_map(usage.get('completion_tokens_details'))
    return contracts.Usage(
        input_tokens=_tokens(usage, 'prompt_tokens', required=True),
        output_tokens=_tokens(usage, 'completion_tokens', required=True),
        cache_read_tokens=_tokens(
            {
                'cached': details.get('cached_tokens')
                if details.get('cached_tokens') is not None
                else usage.get('cache_read_input_tokens')
            },
            'cached',
            required=False,
        ),
        cache_write_tokens=_tokens(
            {
                'cached': details.get('cache_creation_tokens')
                if details.get('cache_creation_tokens') is not None
                else usage.get('cache_creation_input_tokens')
            },
            'cached',
            required=False,
        ),
        reasoning_tokens=_tokens(
            {
                'reasoning': completion.get('reasoning_tokens')
                if completion.get('reasoning_tokens') is not None
                else usage.get('reasoning_tokens')
            },
            'reasoning',
            required=False,
        ),
    )


def _assistant(response: Mapping[str, object]) -> contracts.Message:
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError('Expected one provider choice')
    choice = config.mapping(choices[0], 'choice')
    message = config.mapping(choice.get('message'), 'assistant message')
    text = message.get('content')
    if text is not None and not isinstance(text, str):
        raise ValueError('Expected text from provider')
    raw_calls = message.get('tool_calls')
    calls: list[contracts.ToolCall] = []
    if raw_calls is not None:
        if not isinstance(raw_calls, list):
            raise ValueError('Expected provider tool calls')
        for item in raw_calls:
            call = config.mapping(item, 'tool call')
            function = config.mapping(call.get('function'), 'function call')
            calls.append(
                contracts.ToolCall(
                    _str(call, 'id'),
                    _str(function, 'name'),
                    _str(function, 'arguments'),
                    cast(
                        dict[str, contracts.JSON],
                        _optional_map(call.get('provider_specific_fields')),
                    ),
                )
            )
    return contracts.Message(
        'assistant',
        text,
        tuple(calls),
        provider_fields=cast(
            dict[str, contracts.JSON],
            _optional_map(message.get('provider_specific_fields')),
        ),
    )


class _Wire:
    """Enforce one HTTP dispatch and check the actual provider output cap."""

    def __init__(self, provider: str, output_cap: int) -> None:
        self.provider = provider
        self.output_cap = output_cap
        self.sent = False
        self.response: httpx.Response | httpx2.Response | None = None
        self.violation: str | None = None

    def before(self, request: httpx.Request | httpx2.Request) -> None:
        if self.sent:
            self.violation = 'LiteLLM attempted a second provider dispatch'
            raise RuntimeError(self.violation)
        try:
            body = config.mapping(json.loads(request.content), 'request body')
            if self.provider == 'openai' and (
                body.get('service_tier') != 'default' or body.get('n', 1) != 1
            ):
                raise ValueError('Expected one standard-tier completion')
            if self.provider == 'gemini':
                body = config.mapping(
                    body.get('generationConfig'), 'generation'
                )
                if (
                    body.get('candidateCount', body.get('candidate_count', 1))
                    != 1
                ):
                    raise ValueError('Expected one Gemini candidate')
            if body.get('temperature') != 0:
                raise ValueError('Expected zero temperature')
            cap = body.get(
                'max_completion_tokens',
                body.get(
                    'max_tokens',
                    body.get('max_output_tokens', body.get('maxOutputTokens')),
                ),
            )
        except (ValueError, UnicodeError):
            cap = None
        if (
            request.method != 'POST'
            or type(cap) is not int
            or not (0 < cap <= self.output_cap)
        ):
            self.violation = 'Provider request violated guarded chat settings'
            raise RuntimeError(self.violation)
        self.sent = True

    def after(self, response: httpx.Response | httpx2.Response) -> None:
        self.response = response

    def request_id(self) -> str | None:
        if self.response is None:
            return None
        for name in (
            'x-request-id',
            'request-id',
            'x-goog-request-id',
            'x-guploader-uploadid',
        ):
            value = self.response.headers.get(name)
            if isinstance(value, str) and value:
                return value
        return None

    def actual_model(self) -> str | None:
        if self.response is None:
            return None
        try:
            raw = config.mapping(self.response.json(), 'provider response')
        except (
            ValueError,
            UnicodeError,
            httpx.ResponseNotRead,
            httpx2.ResponseNotRead,
        ):
            return None
        model = raw.get(
            'modelVersion' if self.provider == 'gemini' else 'model'
        )
        return model if isinstance(model, str) and model else None

    def failure(self, exc: Exception) -> client.TransportFailure:
        status = (
            self.response.status_code if self.response is not None else None
        )
        retryable = (
            status in {408, 409, 425, 429}
            or (status is not None and 500 <= status < 600)
            or (
                status is None
                and self.sent
                and isinstance(exc, (httpx.TransportError, TimeoutError))
            )
        )
        unbilled = not self.sent or status in {400, 401, 403, 404, 422, 429}
        retry_after = (
            self.response.headers.get('retry-after')
            if self.response is not None
            else None
        )
        reason = self.violation or (
            f'Provider HTTP {status}'
            if status and status >= 400
            else f'Provider response unavailable ({type(exc).__name__})'
        )
        return client.TransportFailure(
            reason,
            retryable=retryable and self.violation is None,
            unbilled=unbilled,
            retry_after=retry_after,
            request_id=self.request_id(),
            served_model=self.actual_model(),
        )


class LiteLLMTransport:
    """Send paid calls through LiteLLM with injectable HTTP."""

    def __init__(
        self,
        http_transport: httpx.BaseTransport | None = None,
        *,
        openai_transport: httpx2.BaseTransport | None = None,
    ) -> None:
        """Optionally supply mock HTTP transports for the SDKs."""
        self._http_transport = http_transport
        self._openai_transport = openai_transport

    def input_bound(
        self, spec: config.ModelSpec, request: contracts.ChatRequest
    ) -> int | None:
        """Add nothing: LiteLLM counts are estimates, not upper bounds.

        The client already bounds every request by its UTF-8 size; see
        `client.request_input_bound`.
        """
        return None

    def complete(
        self,
        spec: config.ModelSpec,
        request: contracts.ChatRequest,
        *,
        api_key: str,
        cache: bool,
        timeout: float,
    ) -> contracts.ProviderResult:
        """Send one capped provider call and normalize its reported usage."""
        # Lazy imports keep model inventory and the scripted fake offline.
        import litellm
        import openai
        from litellm import exceptions
        from litellm.llms.custom_httpx import http_handler

        wire = _Wire(spec.provider, request.max_output_tokens)
        tools = [
            {
                'type': 'function',
                'function': {
                    'name': tool.name,
                    'description': tool.description,
                    'parameters': dict(tool.parameters),
                },
            }
            for tool in request.tools
        ]
        try:
            with ExitStack() as stack:
                hooks: dict[str, list[Callable[..., None]]] = {
                    'request': [wire.before],
                    'response': [wire.after],
                }
                sdk: object
                if spec.provider == 'openai':
                    openai_http = stack.enter_context(
                        httpx2.Client(
                            timeout=timeout,
                            follow_redirects=False,
                            event_hooks=hooks,
                            transport=self._openai_transport,
                        )
                    )
                    # LiteLLM calls the SDK using the guarded HTTP client.
                    sdk = openai.OpenAI(
                        api_key=api_key,
                        base_url='https://api.openai.com/v1',
                        max_retries=0,
                        timeout=timeout,
                        http_client=openai_http,
                    )
                else:
                    http = stack.enter_context(
                        httpx.Client(
                            timeout=timeout,
                            follow_redirects=False,
                            event_hooks=hooks,
                            transport=self._http_transport,
                        )
                    )
                    sdk = http_handler.HTTPHandler(client=http, timeout=timeout)
                if litellm.model_fallbacks or litellm.model_alias_map:
                    raise ValueError('External LiteLLM routing is unsupported')
                output_arg = (
                    {
                        'max_completion_tokens': request.max_output_tokens,
                        'service_tier': 'default',
                    }
                    if spec.provider == 'openai'
                    else {'max_tokens': request.max_output_tokens}
                )
                response = litellm.completion(
                    model=spec.id,
                    messages=_messages(
                        request, cache and spec.provider == 'anthropic'
                    ),
                    tools=tools or None,
                    api_key=api_key,
                    client=sdk,
                    temperature=0,
                    stream=False,
                    timeout=timeout,
                    num_retries=0,
                    max_retries=0,
                    caching=False,
                    no_log=True,
                    drop_params=False,
                    **output_arg,
                )
                if wire.violation:
                    raise RuntimeError(wire.violation)
                if (
                    not isinstance(response, litellm.ModelResponse)
                    or not wire.sent
                ):
                    raise ValueError(
                        'Expected one non-streaming provider response'
                    )
                data: dict[str, object] = response.model_dump()
                served = wire.actual_model()
                if not served:
                    raise ValueError(
                        'Provider did not identify the served model'
                    )
                return contracts.ProviderResult(
                    _assistant(data),
                    _usage(data),
                    served,
                    wire.request_id(),
                )
        except (
            exceptions.APIConnectionError,
            exceptions.Timeout,
            openai.APIConnectionError,
        ) as exc:
            failure = wire.failure(exc)
            if failure.retryable or wire.violation:
                raise failure from None
            raise client.TransportFailure(
                str(failure),
                retryable=wire.sent,
                unbilled=not wire.sent,
                request_id=failure.request_id,
                served_model=failure.served_model,
            ) from None
        except Exception as exc:
            # The SDK raises provider-specific errors here. Do not expose its
            # raw exceptions, which can contain prompts, responses or API keys.
            raise wire.failure(exc) from None
