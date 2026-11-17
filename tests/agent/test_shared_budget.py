"""Multi-mode budget: role allowances drawn from the run's limits."""

import threading
from concurrent import futures
from decimal import Decimal

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import state

_LIMITS = config.Limits(tool_calls=15, model_turns=30, cost_usd=Decimal('1'))


def _shared(
    limits: config.Limits = _LIMITS, now: list[float] | None = None
) -> state.SharedBudget:
    clock = now if now is not None else [0.0]
    return state.SharedBudget(limits, clock=lambda: clock[0])


def test_allocate_grants_the_request_and_shrinks_the_pool() -> None:
    """A grant is promised to its role and leaves the pool."""
    shared = _shared()

    lead = shared.allocate('lead', tool_calls=3, model_turns=6)

    assert lead.tool_calls_left() == 3
    assert shared.pool().tool_calls == 12
    assert shared.pool().model_turns == 24


def test_allocate_beyond_the_pool_is_clamped() -> None:
    """No grant can promise more than the pool holds."""
    shared = _shared(config.Limits(tool_calls=4))
    shared.allocate('lead', tool_calls=3)

    tester = shared.allocate('tester', tool_calls=3, hypothesis_id='H1')

    assert tester.tool_calls_left() == 1


def test_allocate_none_takes_everything_left() -> None:
    """Asking for None takes all the pool has."""
    shared = _shared()
    shared.allocate('lead', tool_calls=5, model_turns=10)

    report = shared.allocate('lead')

    assert (report.tool_calls_left(), report.model_turns_left()) == (10, 20)


def test_allocate_negative_amount_raises() -> None:
    """A negative request is a programming error."""
    with pytest.raises(ValueError, match='nonnegative'):
        _shared().allocate('lead', tool_calls=-1)


def test_allowance_refuses_tool_call_once_its_share_is_used() -> None:
    """A role cannot spend past its own grant, whatever the pool holds."""
    tester = _shared().allocate('tester', tool_calls=1)
    tester.charge_tool_call()

    assert tester.charge_tool_call() is False


def test_close_returns_unused_share_to_the_pool() -> None:
    """A tester that finishes early gives back what it did not use."""
    shared = _shared()
    tester = shared.allocate('tester', tool_calls=3, model_turns=5)
    tester.charge_tool_call()
    tester.charge_turn(Decimal('0.1'))

    tester.close()

    assert shared.pool().tool_calls == 14
    assert shared.pool().model_turns == 29
    assert shared.pool().cost_usd == Decimal('0.9')
    assert tester.tool_calls_left() == 0


def test_top_up_moves_the_pool_into_an_open_allowance() -> None:
    """Returned capacity can be handed to a later role."""
    shared = _shared()
    report = shared.allocate('lead', tool_calls=1, model_turns=1)
    tester = shared.allocate('tester', tool_calls=3, model_turns=3)
    tester.close()
    rest = shared.allocate('skeptic', tool_calls=10, model_turns=20)

    shared.top_up(report)

    assert report.tool_calls_left() == 1 + 15 - 1 - 10
    assert rest.tool_calls_left() == 10


def test_top_up_of_closed_allowance_raises() -> None:
    """Only an open allowance can receive more."""
    shared = _shared()
    tester = shared.allocate('tester', tool_calls=1)
    tester.close()

    with pytest.raises(ValueError, match='open allowance'):
        shared.top_up(tester)


def test_charges_count_in_the_run_totals() -> None:
    """Every role charge also counts against the shared limits."""
    shared = _shared()
    lead = shared.allocate('lead', tool_calls=2, model_turns=2)

    lead.charge_tool_call()
    lead.charge_turn(Decimal('0.25'))

    assert shared.to_dict()['tool_calls'] == 1
    assert shared.to_dict()['model_turns'] == 1
    assert shared.to_dict()['cost_usd'] == '0.25'


def test_threads_never_exceed_the_run_tool_call_limit() -> None:
    """Testers charging at once can never take more than the limits."""
    shared = _shared()
    testers = [
        shared.allocate('tester', tool_calls=6, hypothesis_id=f'H{n}')
        for n in (1, 2, 3)
    ]
    gate = threading.Barrier(len(testers))

    def spend(allowance: state.RoleAllowance) -> int:
        gate.wait()
        return sum(allowance.charge_tool_call() for _ in range(10))

    with futures.ThreadPoolExecutor(len(testers)) as pool:
        charged = sum(pool.map(spend, testers))

    assert charged == _LIMITS.tool_calls
    assert shared.to_dict()['tool_calls'] == _LIMITS.tool_calls


@pytest.mark.parametrize(
    ('turns', 'cost', 'run_elapsed', 'expected'),
    [
        pytest.param(0, '0', 0.0, None, id='fresh'),
        pytest.param(5, '0', 0.0, 'model_turn_limit', id='turns'),
        pytest.param(1, '0.5', 0.0, 'cost_cap', id='cost'),
        pytest.param(1, '0', 100.0, 'role_wall_time', id='role-time'),
        pytest.param(1, '0', 600.0, 'wall_time', id='run-time'),
    ],
)
def test_allowance_stop_reason_names_the_exhausted_limit(
    turns: int, cost: str, run_elapsed: float, expected: str | None
) -> None:
    """Role limits and run limits are each reported by name."""
    now = [0.0]
    shared = _shared(now=now)
    tester = shared.allocate(
        'tester', model_turns=5, cost_usd=Decimal('0.5'), wall_time_s=100.0
    )
    tester.used.model_turns = turns
    tester.used.cost_usd = Decimal(cost)
    now[0] = run_elapsed

    assert tester.stop_reason() == expected


def test_role_wall_time_is_not_a_hard_stop() -> None:
    """One tester running out of time does not end the run."""
    assert 'role_wall_time' not in state.HARD_STOPS


def test_allowance_to_dict_names_role_and_hypothesis() -> None:
    """The record shows whose allowance it is."""
    tester = _shared().allocate('tester', tool_calls=2, hypothesis_id='H3')

    data = tester.to_dict()

    assert (data['role'], data['hypothesis_id']) == ('tester', 'H3')
    assert data['granted'] == {
        'tool_calls': 2,
        'model_turns': 30,
        'cost_usd': '1',
    }
