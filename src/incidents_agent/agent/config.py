"""Agent settings: limits, verification reserve, ablation and context."""

import dataclasses
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from incidents_agent.llm import config as llm_config

DEFAULT_PATH = Path('config/agent.yaml')


@dataclasses.dataclass(frozen=True)
class Limits:
    """Per-case limits and the share set aside for verification.

    Attributes:
        tool_calls: Total tool calls, agent bookkeeping tools included.
        model_turns: Total model calls.
        wall_time_s: Wall-clock seconds for the whole run.
        cost_usd: Settled model cost for the whole run.
        verify_tool_calls: Tool calls reserved for the verify step.
        verify_model_turns: Model turns reserved for verify and correction.
        verify_cost_usd: Cost reserved for verify and correction.
        correction_model_turns: Model turns allowed for the single return
            to investigation after a refutation.
    """

    tool_calls: int = 15
    model_turns: int = 30
    wall_time_s: float = 600.0
    cost_usd: Decimal = Decimal('0.05')
    verify_tool_calls: int = 2
    verify_model_turns: int = 4
    verify_cost_usd: Decimal = Decimal('0.01')
    correction_model_turns: int = 3

    def __post_init__(self) -> None:
        """Reject negative or inconsistent limits."""
        counts = (
            self.tool_calls,
            self.model_turns,
            self.verify_tool_calls,
            self.verify_model_turns,
            self.correction_model_turns,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError('Limit counts must be nonnegative integers')
        if self.wall_time_s <= 0:
            raise ValueError('wall_time_s must be positive')
        if self.cost_usd < 0 or self.verify_cost_usd < 0:
            raise ValueError('Cost limits must be nonnegative')

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        data = dataclasses.asdict(self)
        data['cost_usd'] = str(self.cost_usd)
        data['verify_cost_usd'] = str(self.verify_cost_usd)
        return data


@dataclasses.dataclass(frozen=True)
class ContextPolicy:
    """How older tool results are replaced by digests.

    Attributes:
        keep_recent_results: Data-tool results always kept in full.
        digest_block: Older results are digested this many at a time, so the
            conversation prefix stays the same between collapses.
        digest_lines: Lines of the result kept in a digest.
        digest_line_chars: Characters kept per digest line.
    """

    keep_recent_results: int = 3
    digest_block: int = 3
    digest_lines: int = 3
    digest_line_chars: int = 160

    def __post_init__(self) -> None:
        """Reject values that would never keep or never digest results."""
        if self.keep_recent_results < 1 or self.digest_block < 1:
            raise ValueError('keep_recent_results and digest_block must be >0')
        if self.digest_lines < 1 or self.digest_line_chars < 20:
            raise ValueError('Digests must keep at least one 20-char line')

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    """Everything that configures one agent run.

    Attributes:
        model_alias: Alias in `config/models.yaml`; empty uses its default.
        max_output_tokens: Output cap for each model call.
        ledger: Ablation switch for the hypothesis ledger.
        verify: Ablation switch for the verify step.
        limits: Per-case limits.
        context: Context policy.
    """

    model_alias: str = ''
    max_output_tokens: int = 1024
    ledger: bool = True
    verify: bool = True
    limits: Limits = dataclasses.field(default_factory=Limits)
    context: ContextPolicy = dataclasses.field(default_factory=ContextPolicy)

    def __post_init__(self) -> None:
        """Check the output cap."""
        if type(self.max_output_tokens) is not int or (
            self.max_output_tokens <= 0
        ):
            raise ValueError('max_output_tokens must be a positive integer')

    @property
    def plain_loop(self) -> bool:
        """Return True when both ablation switches are off."""
        return not self.ledger and not self.verify

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-ready dictionary for manifests and fingerprints."""
        return {
            'model_alias': self.model_alias,
            'max_output_tokens': self.max_output_tokens,
            'ledger': self.ledger,
            'verify': self.verify,
            'limits': self.limits.to_dict(),
            'context': self.context.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> 'AgentConfig':
        """Build a configuration from a parsed mapping.

        Args:
            data: Mapping with the fields of `config/agent.yaml`.

        Returns:
            The validated configuration.

        Raises:
            ConfigError: A field is unknown or has the wrong type.
        """
        known = {
            'model_alias',
            'max_output_tokens',
            'ledger',
            'verify',
            'limits',
            'context',
        }
        unknown = set(data) - known
        if unknown:
            raise llm_config.ConfigError(
                f'Unknown agent fields: {sorted(unknown)}'
            )
        alias = data.get('model_alias')
        if alias is not None and not isinstance(alias, str):
            raise llm_config.ConfigError('model_alias must be a string')
        for name in ('ledger', 'verify'):
            if not isinstance(data.get(name, True), bool):
                raise llm_config.ConfigError(f'{name} must be true or false')
        try:
            return cls(
                model_alias=alias or '',
                max_output_tokens=_int(data.get('max_output_tokens', 1024)),
                ledger=bool(data.get('ledger', True)),
                verify=bool(data.get('verify', True)),
                limits=_limits(
                    llm_config.mapping(data.get('limits', {}), 'limits')
                ),
                context=_context(
                    llm_config.mapping(data.get('context', {}), 'context')
                ),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, llm_config.ConfigError):
                raise
            raise llm_config.ConfigError(str(exc)) from exc


def _int(value: object) -> int:
    if type(value) is not int:
        raise llm_config.ConfigError(f'Expected an integer, got {value!r}')
    return value


def _limits(data: Mapping[str, object]) -> Limits:
    defaults = Limits()
    fields = {field.name for field in dataclasses.fields(Limits)}
    if set(data) - fields:
        raise llm_config.ConfigError(f'Unknown limits: {sorted(set(data))}')
    wall = data.get('wall_time_s', defaults.wall_time_s)
    if isinstance(wall, bool) or not isinstance(wall, int | float | Decimal):
        raise llm_config.ConfigError('wall_time_s must be a number')
    return Limits(
        tool_calls=_int(data.get('tool_calls', defaults.tool_calls)),
        model_turns=_int(data.get('model_turns', defaults.model_turns)),
        wall_time_s=float(wall),
        cost_usd=llm_config.money(
            data.get('cost_usd', defaults.cost_usd), 'cost_usd'
        ),
        verify_tool_calls=_int(
            data.get('verify_tool_calls', defaults.verify_tool_calls)
        ),
        verify_model_turns=_int(
            data.get('verify_model_turns', defaults.verify_model_turns)
        ),
        verify_cost_usd=llm_config.money(
            data.get('verify_cost_usd', defaults.verify_cost_usd),
            'verify_cost_usd',
        ),
        correction_model_turns=_int(
            data.get('correction_model_turns', defaults.correction_model_turns)
        ),
    )


def _context(data: Mapping[str, object]) -> ContextPolicy:
    fields = {field.name for field in dataclasses.fields(ContextPolicy)}
    if set(data) - fields:
        raise llm_config.ConfigError(f'Unknown context: {sorted(set(data))}')
    defaults = ContextPolicy()
    return ContextPolicy(
        **{
            name: _int(data.get(name, getattr(defaults, name)))
            for name in fields
        }
    )


def load_agent_config(path: Path = DEFAULT_PATH) -> AgentConfig:
    """Read and validate the agent configuration file.

    Args:
        path: YAML file in the format of `config/agent.yaml`.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: The file is malformed or has invalid values.
        OSError: The file cannot be read.
    """
    return AgentConfig.from_dict(llm_config.read_mapping(path))
