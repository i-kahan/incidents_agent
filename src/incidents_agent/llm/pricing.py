"""Conservative admission and actual pricing from the bundled LiteLLM map."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from decimal import localcontext
from importlib import metadata
from typing import NamedTuple

from incidents_agent.llm import config
from incidents_agent.llm import contracts

_MILLION = Decimal(1_000_000)
_RATE_KEYS = (
    'input_cost_per_token',
    'output_cost_per_token',
    'cache_read_input_token_cost',
    'cache_creation_input_token_cost',
    'output_cost_per_reasoning_token',
)
_TIER = re.compile(r'_above_(\d+)(k?)_tokens$')


class Rates(NamedTuple):
    """USD per token for the normal and special usage categories."""

    input: Decimal
    output: Decimal
    read: Decimal
    write: Decimal
    reasoning: Decimal

    def cost(self, usage: contracts.Usage) -> Decimal:
        """Price one normalized provider usage record."""
        with localcontext(prec=48):
            return (
                (
                    usage.input_tokens
                    - usage.cache_read_tokens
                    - usage.cache_write_tokens
                )
                * self.input
                + usage.cache_read_tokens * self.read
                + usage.cache_write_tokens * self.write
                + (usage.output_tokens - usage.reasoning_tokens) * self.output
                + usage.reasoning_tokens * self.reasoning
            )


@dataclass(frozen=True, slots=True)
class Price:
    """A model's hard input ceiling, caching feature, and token rates."""

    input_ceiling: int
    tiers: tuple[tuple[int, Rates], ...]
    cache_supported: bool
    output_ceiling: int | None = None

    def bound(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Reserve at the highest applicable rate for each side."""
        applicable = [
            r for threshold, r in self.tiers if input_tokens > threshold
        ]
        with localcontext(prec=48):
            return input_tokens * max(
                max(r.input, r.read, r.write) for r in applicable
            ) + output_tokens * max(
                max(r.output, r.reasoning) for r in applicable
            )

    def cost(self, usage: contracts.Usage) -> Decimal:
        """Settle using the tier crossed by the provider's usage."""
        rates = self.tiers[0][1]
        for threshold, tier in self.tiers[1:]:
            if usage.input_tokens <= threshold:
                break
            rates = tier
        return rates.cost(usage)


def bundled_catalog() -> dict[str, object]:
    """Load the locked distribution's prices without network or SDK import."""
    path = metadata.distribution('litellm').locate_file(
        'litellm/model_prices_and_context_window_backup.json'
    )
    return config.mapping(
        json.loads(path.read_text(), parse_float=Decimal),
        'LiteLLM price map',
    )


def _rate(
    raw: Mapping[str, object], key: str, fallback: Decimal | None = None
) -> Decimal:
    value = raw.get(key)
    if value is None and fallback is not None:
        return fallback
    if value is None:
        raise contracts.ModelUnavailable(f'Missing LiteLLM rate: {key}')
    rate = config.money(value, key)
    exponent = rate.as_tuple().exponent
    assert isinstance(exponent, int)
    if exponent < -18:
        raise config.ConfigError(f'{key} exceeds accounting precision')
    return rate


def _tiers(
    raw: Mapping[str, object], cache: bool
) -> tuple[tuple[int, Rates], ...]:
    thresholds = {-1: ''}
    for key in raw:
        if any(key.startswith(f'{base}_above_') for base in _RATE_KEYS):
            match = _TIER.search(key)
            if match and key[: match.start()] in _RATE_KEYS:
                threshold = int(match[1]) * (1000 if match[2] else 1)
                thresholds[threshold] = match[0]
    input_key, output_key, read_key, write_key, reasoning_key = _RATE_KEYS
    tiers: list[tuple[int, Rates]] = []
    for threshold, suffix in sorted(thresholds.items()):
        input_rate = _rate(raw, input_key + suffix, _rate(raw, input_key))
        output_rate = _rate(raw, output_key + suffix, _rate(raw, output_key))
        read_rate = _rate(
            raw, read_key + suffix, _rate(raw, read_key, input_rate)
        )
        # Only Anthropic creates cache entries on the request path we support.
        write_rate = (
            _rate(raw, write_key + suffix, _rate(raw, write_key))
            if cache and raw.get('litellm_provider') == 'anthropic'
            else input_rate
        )
        reasoning_rate = _rate(
            raw,
            reasoning_key + suffix,
            _rate(raw, reasoning_key, output_rate),
        )
        tiers.append(
            (
                threshold,
                Rates(
                    input_rate,
                    output_rate,
                    read_rate,
                    write_rate,
                    reasoning_rate,
                ),
            )
        )
    return tuple(tiers)


def _override_rates(
    override: config.PriceOverride, raw: Mapping[str, object], cache: bool
) -> Rates:
    base_input, base_output = (
        override.input / _MILLION,
        override.output / _MILLION,
    )
    read = override.cache_read
    write = override.cache_write
    return Rates(
        base_input,
        base_output,
        read / _MILLION
        if read is not None
        else _rate(raw, _RATE_KEYS[2], base_input),
        write / _MILLION
        if write is not None
        else (_rate(raw, _RATE_KEYS[3]) if cache else base_input),
        base_output,
    )


def _record(
    spec: config.ModelSpec, catalog: Mapping[str, object]
) -> dict[str, object]:
    for key in (spec.id, spec.id.split('/', 1)[-1]):
        raw = catalog.get(key)
        if (
            isinstance(raw, dict)
            and raw.get('litellm_provider') == spec.provider
        ):
            return config.mapping(raw, key)
    return {}


def resolve(spec: config.ModelSpec, catalog: Mapping[str, object]) -> Price:
    """Resolve known prices and published caps for the exact provider route."""
    raw = _record(spec, catalog)
    modes = raw.get('supported_output_modalities')
    if raw.get('mode', 'chat') != 'chat' or (
        modes is not None and modes != ['text']
    ):
        raise contracts.ModelUnavailable('Requires a text-only chat model')
    ceiling = raw.get('max_input_tokens', spec.max_input_tokens)
    if type(ceiling) is not int or not 0 < ceiling <= 100_000_000:
        if spec.override is None:
            _rate(raw, _RATE_KEYS[0])
        raise contracts.ModelUnavailable('No known hard input token ceiling')
    output = raw.get('max_output_tokens')
    cache = raw.get('supports_prompt_caching') is True
    try:
        if spec.override is None:
            tiers = _tiers(raw, cache)
        else:
            tiers = (
                (
                    -1,
                    _override_rates(
                        spec.override,
                        raw,
                        cache and spec.provider == 'anthropic',
                    ),
                ),
            )
    except config.ConfigError as exc:
        raise contracts.ModelUnavailable(str(exc)) from exc
    return Price(ceiling, tiers, cache, output if type(output) is int else None)
