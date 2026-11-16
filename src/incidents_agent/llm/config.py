"""Validated owner configuration for model aliases and spending."""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from decimal import localcontext
from pathlib import Path
from typing import Final

import dotenv
import yaml

LOGGER = logging.getLogger(__name__)
PROVIDER_KEYS: Final = {
    'openai': 'OPENAI_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'gemini': 'GEMINI_API_KEY',
}
BUDGET_ENV: Final = 'INCIDENTS_BUDGET_USD'
_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*')
_PIN = re.compile(r'(?:-\d{4}-\d{2}-\d{2}|-\d{8}|-\d{3})$')


class ConfigError(ValueError):
    """A local configuration file or override is invalid."""


class _Loader(yaml.SafeLoader):
    """Reject duplicate keys and preserve the spelling of decimal prices."""


def _mapping(loader: _Loader, node: yaml.MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key: object = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigError(f'Invalid or duplicate YAML key: {key!r}')
        result[key] = loader.construct_object(value_node)
    return result


def _decimal(loader: _Loader, node: yaml.ScalarNode) -> Decimal:
    return money(loader.construct_scalar(node), 'YAML number')


_Loader.add_constructor('tag:yaml.org,2002:map', _mapping)
_Loader.add_constructor('tag:yaml.org,2002:float', _decimal)


def read_mapping(path: Path) -> dict[str, object]:
    """Read a YAML mapping with strictly named keys."""
    try:
        # The safe loader above only adds constructors for maps and numbers.
        raw: object = yaml.load(
            path.read_text(encoding='utf-8'), Loader=_Loader
        )
    except (OSError, UnicodeError, yaml.YAMLError, ConfigError) as exc:
        raise ConfigError(f'{path}: {exc}') from exc
    return mapping(raw, str(path))


def mapping(data: object, field: str) -> dict[str, object]:
    """Validate a mapping from an external source."""
    if not isinstance(data, dict):
        raise ConfigError(f'{field} must be a mapping')
    result: dict[str, object] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ConfigError(f'{field} must use string keys')
        result[key] = value
    return result


def money(value: object, field: str) -> Decimal:
    """Parse a finite, nonnegative amount within exact accounting precision."""
    if type(value) not in (str, int, float, Decimal):
        raise ConfigError(f'{field} must be a nonnegative decimal')
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f'{field} must be a nonnegative decimal') from exc
    if not parsed.is_finite() or parsed < 0 or parsed >= Decimal('1e9'):
        raise ConfigError(f'{field} is outside supported decimal precision')
    with localcontext(prec=48):
        exact = parsed.quantize(Decimal('1e-18'))
        if exact != parsed:
            raise ConfigError(f'{field} is outside supported decimal precision')
        return exact.normalize()


def environment(
    path: Path, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Read dotenv defaults without mutating process-wide credentials."""
    return {
        **{
            key: value
            for key, value in dotenv.dotenv_values(path).items()
            if value is not None
        },
        **(os.environ if environ is None else environ),
    }


@dataclass(frozen=True, slots=True)
class PriceOverride:
    """Owner supplied flat rates in USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One configured alias and its provider route."""

    alias: str
    id: str
    provider: str
    override: PriceOverride | None = None
    max_input_tokens: int | None = None

    @property
    def placeholder(self) -> bool:
        """Whether the owner still needs to supply a model ID."""
        return self.id.endswith('/<model-id>')

    @property
    def pinned(self) -> bool:
        """Whether the provider ID ends in a date or version suffix."""
        return bool(_PIN.search(self.id))


@dataclass(frozen=True, slots=True)
class Models:
    """Configured models indexed by alias."""

    aliases: Mapping[str, ModelSpec]
    default: str


def _override(data: Mapping[str, object]) -> PriceOverride | None:
    names = (
        'input_cost_per_mtok',
        'output_cost_per_mtok',
        'cached_input_cost_per_mtok',
        'cache_write_cost_per_mtok',
    )
    values = {name: money(data[name], name) for name in names if name in data}
    if not values:
        return None
    if any(name not in values for name in names[:2]):
        raise ConfigError('Price overrides need both input and output rates')
    return PriceOverride(
        values[names[0]],
        values[names[1]],
        values.get(names[2]),
        values.get(names[3]),
    )


def load_models(path: Path) -> Models:
    """Read aliases, routing and optional price or input-ceiling overrides."""
    data = read_mapping(path)
    if set(data) != {'models', 'default'}:
        raise ConfigError('Model config requires models and default')
    allowed = {
        'model',
        'input_cost_per_mtok',
        'output_cost_per_mtok',
        'cached_input_cost_per_mtok',
        'cache_write_cost_per_mtok',
        'max_input_tokens',
    }
    models: dict[str, ModelSpec] = {}
    for alias, raw in mapping(data['models'], 'models').items():
        entry = mapping(raw, alias)
        model = entry.get('model')
        if not _NAME.fullmatch(alias) or set(entry) - allowed:
            raise ConfigError(f'Invalid model entry for {alias!r}')
        if not isinstance(model, str):
            raise ConfigError(f'{alias}: model must be a provider-prefixed ID')
        provider, _, name = model.partition('/')
        if provider not in PROVIDER_KEYS or (
            name != '<model-id>' and not _NAME.fullmatch(name)
        ):
            raise ConfigError(f'{alias}: invalid provider or model ID')
        ceiling = entry.get('max_input_tokens')
        if ceiling is not None and (
            not isinstance(ceiling, int)
            or isinstance(ceiling, bool)
            or not 0 < ceiling <= 100_000_000
        ):
            raise ConfigError(f'{alias}: max_input_tokens must be positive')
        spec = ModelSpec(alias, model, provider, _override(entry), ceiling)
        if not spec.placeholder and not spec.pinned:
            LOGGER.warning('Model %s uses floating ID %s', alias, model)
        models[alias] = spec
    default = data['default']
    if not isinstance(default, str) or default not in models:
        raise ConfigError('default must name a configured model alias')
    return Models(models, default)


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    """The effective invocation budget, before any usage."""

    total: Decimal
    margin: Decimal
    per_case: Decimal | None
    reserves: Mapping[str, Decimal]

    @property
    def limit(self) -> Decimal:
        """Return the maximum spend after the safety margin."""
        return self.total - self.margin


def load_budget(
    path: Path,
    *,
    override: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> BudgetSettings:
    """Read current file settings with a CLI or environment total override."""
    data = read_mapping(path)
    if (
        set(data)
        - {'total_usd', 'safety_margin_usd', 'per_case_cap_usd', 'reserves'}
        or not {'total_usd', 'safety_margin_usd'} <= data.keys()
    ):
        raise ConfigError('Invalid budget fields or missing total and margin')
    env = os.environ if environ is None else environ
    selected = override if override is not None else env.get(BUDGET_ENV)
    total = money(
        data['total_usd'] if selected is None else selected, 'total_usd'
    )
    margin = money(data['safety_margin_usd'], 'safety_margin_usd')
    per_case = data.get('per_case_cap_usd')
    reserves = {
        name: money(value, f'reserve {name}')
        for name, value in mapping(data.get('reserves', {}), 'reserves').items()
    }
    if any(not _NAME.fullmatch(name) for name in reserves):
        raise ConfigError('Invalid reserve name')
    if margin > total or sum(reserves.values()) > total - margin:
        raise ConfigError('Margin or named reserves exceed the spend limit')
    return BudgetSettings(
        total,
        margin,
        None if per_case is None else money(per_case, 'per_case_cap_usd'),
        reserves,
    )
