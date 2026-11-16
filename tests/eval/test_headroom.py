"""Tests for the headroom breakdowns and the cost estimate."""

from collections.abc import Mapping

import pytest

from incidents_agent.eval import headroom

ROWS: list[Mapping[str, object]] = [
    {'case_id': 'c1', 'fault': 'cpu', 'service': 'a', 'service_rank': 1},
    {'case_id': 'c2', 'fault': 'cpu', 'service': 'b', 'service_rank': 2},
    {'case_id': 'c3', 'fault': 'mem', 'service': 'a', 'service_rank': 4},
    {'case_id': 'c4', 'fault': 'mem', 'service': 'b', 'service_rank': None},
]

CONFIG = {
    'models': ['m1'],
    'tool_calls': 15,
    'result_cap_chars': 6000,
    'output_tokens_per_turn': 150,
    'target_usd_per_case': 0.05,
    'probe_calls': [['describe_case', {}]],
}


def test_breakdown_by_fault_scores_each_group() -> None:
    """Accuracy at top 1 and top 3 is computed per fault type."""
    scores = headroom.breakdown(ROWS, 'fault')

    assert scores == {
        'cpu': headroom.GroupScore(n=2, ac1=0.5, ac3=1.0),
        'mem': headroom.GroupScore(n=2, ac1=0.0, ac3=0.0),
    }


def test_misses_at_top1_include_unranked_cases() -> None:
    """A case whose true service was never ranked counts as a miss."""
    assert headroom.misses(ROWS, 1) == ['c2', 'c3', 'c4']


def test_misses_at_top3_exclude_rank_two() -> None:
    """A rank inside the cut-off is not a miss."""
    assert headroom.misses(ROWS, 3) == ['c3', 'c4']


@pytest.mark.parametrize(
    ('chars', 'cap', 'expected'),
    [
        pytest.param(9, None, 3, id='rounds_up'),
        pytest.param(10_000, 6000, 1500, id='capped'),
    ],
)
def test_tokens_from_chars(chars: int, cap: int | None, expected: int) -> None:
    """Characters convert to tokens at four per token, after the cap."""
    assert headroom.tokens_from_chars(chars, cap) == expected


def test_estimate_case_cost_resends_history_each_turn() -> None:
    """Turn t carries the prompt plus t earlier results and outputs."""
    shape = headroom.LoopShape(
        prompt_tokens=100, result_tokens=10, output_tokens=5, tool_calls=2
    )
    prices = headroom.Prices(input=1.0, output=2.0, cache_read=None)

    estimate = headroom.estimate_case_cost(shape, prices)

    assert estimate.input_tokens == 100 + 115 + 130
    assert estimate.output_tokens == 15
    assert estimate.uncached_usd == pytest.approx(345 + 30)
    assert estimate.cached_usd is None


def test_estimate_case_cost_with_cache_reads_earlier_prompts() -> None:
    """Each turn reads the previous turn's prompt at the cache price."""
    shape = headroom.LoopShape(
        prompt_tokens=100, result_tokens=10, output_tokens=5, tool_calls=2
    )
    prices = headroom.Prices(input=1.0, output=2.0, cache_read=0.1)

    estimate = headroom.estimate_case_cost(shape, prices)

    reused = 100 + 115
    assert estimate.cached_usd == pytest.approx(
        reused * 0.1 + (345 - reused) * 1.0 + 30
    )


def test_parse_cost_config_reads_valid_record() -> None:
    """A complete record becomes a typed configuration."""
    config = headroom.parse_cost_config(CONFIG)

    assert config.models == ('m1',)
    assert config.probe_calls == (('describe_case', {}),)


@pytest.mark.parametrize(
    ('key', 'value', 'message'),
    [
        pytest.param('models', 'm1', 'models', id='models_not_list'),
        pytest.param('tool_calls', 0, 'tool_calls', id='zero_calls'),
        pytest.param('probe_calls', [['x']], 'probe_calls', id='bad_call'),
        pytest.param('target_usd_per_case', 'x', 'target', id='bad_target'),
    ],
)
def test_parse_cost_config_with_bad_field_raises(
    key: str, value: object, message: str
) -> None:
    """Each malformed field is rejected by name."""
    record = {**CONFIG, key: value}

    with pytest.raises(ValueError, match=message):
        headroom.parse_cost_config(record)
