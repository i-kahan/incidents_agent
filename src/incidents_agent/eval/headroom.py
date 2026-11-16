"""Headroom analysis: where a baseline fails, and what an agent would cost.

The functions here are pure. They take the per-case rows RCAEval's evaluator
returns and plain numbers, so they can be tested without the dataset.
"""

import dataclasses
from collections.abc import Mapping
from collections.abc import Sequence

CHARS_PER_TOKEN = 4


@dataclasses.dataclass(frozen=True)
class GroupScore:
    """Service-level accuracy over one slice of cases.

    Attributes:
        n: Number of cases in the slice.
        ac1: Share of cases with the true service ranked first.
        ac3: Share of cases with the true service in the top three.
    """

    n: int
    ac1: float
    ac3: float


def _rank(row: Mapping[str, object]) -> int | None:
    rank = row.get('service_rank')
    return rank if isinstance(rank, int) else None


def _hit(row: Mapping[str, object], k: int) -> bool:
    rank = _rank(row)
    return rank is not None and rank <= k


def breakdown(
    rows: Sequence[Mapping[str, object]], key: str
) -> dict[str, GroupScore]:
    """Group per-case rows by a field and score each group.

    Args:
        rows: Per-case rows from the evaluator, with `service_rank`.
        key: The field to group by, for example `fault` or `service`.

    Returns:
        A score per group value, ordered by value.
    """
    groups: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    return {
        value: GroupScore(
            n=len(members),
            ac1=sum(_hit(r, 1) for r in members) / len(members),
            ac3=sum(_hit(r, 3) for r in members) / len(members),
        )
        for value, members in sorted(groups.items())
    }


def misses(rows: Sequence[Mapping[str, object]], k: int) -> list[str]:
    """Return the case ids whose true service is not in the top `k`."""
    return [str(row['case_id']) for row in rows if not _hit(row, k)]


def tokens_from_chars(chars: int, cap_chars: int | None = None) -> int:
    """Estimate tokens from characters, after an optional result cap.

    Args:
        chars: Characters in the text.
        cap_chars: The cap a tool applies to its result, if any.

    Returns:
        Estimated tokens, rounded up.
    """
    if cap_chars is not None:
        chars = min(chars, cap_chars)
    return -(-chars // CHARS_PER_TOKEN)


@dataclasses.dataclass(frozen=True)
class Prices:
    """Per-token prices in US dollars.

    Attributes:
        input: Price of an uncached input token.
        output: Price of an output token.
        cache_read: Price of a cached input token, or None without caching.
    """

    input: float
    output: float
    cache_read: float | None


@dataclasses.dataclass(frozen=True)
class LoopShape:
    """The size of one investigation loop.

    Attributes:
        prompt_tokens: System prompt plus the task, sent on every turn.
        result_tokens: Tokens in one tool result.
        output_tokens: Tokens the model writes per turn.
        tool_calls: Tool calls per case; one more turn submits the answer.
    """

    prompt_tokens: int
    result_tokens: int
    output_tokens: int
    tool_calls: int


@dataclasses.dataclass(frozen=True)
class CostEstimate:
    """Estimated cost of one case.

    Attributes:
        input_tokens: Input tokens over all turns.
        output_tokens: Output tokens over all turns.
        uncached_usd: Cost with no prompt caching.
        cached_usd: Cost when each turn reads the previous turn's prompt
            from cache, or None when the model has no cache price.
    """

    input_tokens: int
    output_tokens: int
    uncached_usd: float
    cached_usd: float | None


def estimate_case_cost(shape: LoopShape, prices: Prices) -> CostEstimate:
    """Estimate one case's cost for a loop that re-sends its history.

    Turn `t` (from 0) sends the prompt plus `t` earlier tool results and
    model turns. Nothing is trimmed, so the estimate is an upper bound for a
    loop that keeps its whole history. Cache writes are priced as ordinary
    input.

    Args:
        shape: The loop's size.
        prices: The model's prices.

    Returns:
        The estimate.
    """
    turns = shape.tool_calls + 1
    step = shape.result_tokens + shape.output_tokens
    prompts = [shape.prompt_tokens + t * step for t in range(turns)]
    input_tokens = sum(prompts)
    output_tokens = turns * shape.output_tokens
    output_usd = output_tokens * prices.output
    uncached = input_tokens * prices.input + output_usd
    cached = None
    if prices.cache_read is not None:
        reused = sum(prompts[:-1])
        cached = (
            reused * prices.cache_read
            + (input_tokens - reused) * prices.input
            + output_usd
        )
    return CostEstimate(input_tokens, output_tokens, uncached, cached)


@dataclasses.dataclass(frozen=True)
class CostConfig:
    """Inputs to the cost estimate, read from `config/cost_estimate.json`.

    Attributes:
        models: LiteLLM model names to price.
        tool_calls: Tool calls per case.
        result_cap_chars: The cap each tool result is cut to.
        output_tokens_per_turn: Assumed model output per turn.
        target_usd_per_case: The cost per case the budget allows.
        probe_calls: Reference-toolkit calls whose results are measured.
    """

    models: tuple[str, ...]
    tool_calls: int
    result_cap_chars: int
    output_tokens_per_turn: int
    target_usd_per_case: float
    probe_calls: tuple[tuple[str, dict[str, object]], ...]


def parse_cost_config(record: Mapping[str, object]) -> CostConfig:
    """Validate and read the cost-estimate configuration.

    Args:
        record: The parsed JSON object.

    Returns:
        The configuration.

    Raises:
        ValueError: A field is missing or has the wrong type.
    """
    models, calls = record.get('models'), record.get('probe_calls')
    if not isinstance(models, list) or not all(
        isinstance(m, str) for m in models
    ):
        raise ValueError('models must be a list of strings')
    if not isinstance(calls, list) or not all(
        isinstance(c, list)
        and len(c) == 2
        and isinstance(c[0], str)
        and isinstance(c[1], dict)
        for c in calls
    ):
        raise ValueError('probe_calls must be [name, arguments] pairs')
    numbers = {}
    for key in ('tool_calls', 'result_cap_chars', 'output_tokens_per_turn'):
        value = record.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f'{key} must be a positive integer')
        numbers[key] = value
    target = record.get('target_usd_per_case')
    if not isinstance(target, (int, float)):
        raise ValueError('target_usd_per_case must be a number')
    return CostConfig(
        models=tuple(models),
        tool_calls=numbers['tool_calls'],
        result_cap_chars=numbers['result_cap_chars'],
        output_tokens_per_turn=numbers['output_tokens_per_turn'],
        target_usd_per_case=float(target),
        probe_calls=tuple((c[0], dict(c[1])) for c in calls),
    )
