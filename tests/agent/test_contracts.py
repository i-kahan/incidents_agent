"""Tests for the agent configuration, task, budget, trajectory and report."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import report
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.llm import config as llm_config
from incidents_agent.llm import contracts

_REPO = Path(__file__).resolve().parents[2]


def test_load_agent_config_reads_repo_file() -> None:
    """Load agent config reads repo file."""
    loaded = config.load_agent_config(_REPO / 'config' / 'agent.yaml')

    assert loaded.limits.tool_calls == 15
    assert loaded.limits.cost_usd == Decimal('0.05')
    assert loaded.ledger and loaded.verify


@pytest.mark.parametrize(
    'text',
    [
        pytest.param('unknown: 1\n', id='unknown_field'),
        pytest.param('limits:\n  tool_calls: -1\n', id='negative_calls'),
        pytest.param('limits:\n  tool_calls: 1.5\n', id='fractional_calls'),
        pytest.param('ledger: maybe\n', id='non_bool_switch'),
        pytest.param('context:\n  digest_block: 0\n', id='zero_block'),
    ],
)
def test_load_agent_config_with_invalid_value_raises(
    tmp_path: Path, text: str
) -> None:
    """Load agent config with invalid value raises."""
    path = tmp_path / 'agent.yaml'
    path.write_text(text, encoding='utf-8')

    with pytest.raises(llm_config.ConfigError):
        config.load_agent_config(path)


def test_agent_config_round_trips_through_dict() -> None:
    """Agent config round trips through dict."""
    original = config.AgentConfig(
        model_alias='small',
        ledger=False,
        limits=config.Limits(tool_calls=9, cost_usd=Decimal('0.02')),
    )

    rebuilt = config.AgentConfig.from_dict(
        json.loads(json.dumps(original.to_dict()))
    )

    assert rebuilt == original


def test_budget_holds_verify_reserve_until_released() -> None:
    """Budget holds verify reserve until released."""
    budget = state.Budget(config.Limits(tool_calls=5), reserve=True)

    before = budget.tool_calls_left()
    budget.release_reserve()

    assert (before, budget.tool_calls_left()) == (3, 5)


def test_budget_refuses_tool_call_when_share_used() -> None:
    """Budget refuses tool call when share used."""
    budget = state.Budget(
        config.Limits(tool_calls=3, verify_tool_calls=2), reserve=True
    )
    budget.charge_tool_call()

    assert budget.charge_tool_call() is False


def test_budget_reservation_fails_when_reserve_takes_everything() -> None:
    """Budget reservation fails when reserve takes everything."""
    budget = state.Budget(
        config.Limits(tool_calls=2, verify_tool_calls=2), reserve=True
    )

    assert budget.reservation_ok() is False


@pytest.mark.parametrize(
    ('turns', 'cost', 'elapsed', 'expected'),
    [
        pytest.param(0, '0', 0.0, None, id='fresh'),
        pytest.param(26, '0', 0.0, 'model_turn_limit', id='turns'),
        pytest.param(1, '0.04', 0.0, 'cost_cap', id='cost'),
        pytest.param(1, '0', 600.0, 'wall_time', id='time'),
    ],
)
def test_budget_stop_reason_names_the_exhausted_limit(
    turns: int, cost: str, elapsed: float, expected: str | None
) -> None:
    """Each exhausted limit is reported as its own stop reason."""
    now = [0.0]
    budget = state.Budget(config.Limits(), reserve=True, clock=lambda: now[0])
    budget.model_turns = turns
    budget.cost_usd = Decimal(cost)
    now[0] = elapsed

    assert budget.stop_reason() == expected


def test_task_from_task_json_reads_blind_view(tmp_path: Path) -> None:
    """Task from task json reads blind view."""
    path = tmp_path / 'task.json'
    path.write_text(
        json.dumps(
            {
                'case_id': 'c-1',
                'inject_time': 1700000000,
                'candidate_services': ['a', 'b'],
                'candidate_indicators': ['a_cpu'],
            }
        ),
        encoding='utf-8',
    )

    task = state.Task.from_task_json(path)

    assert task == state.Task(
        tmp_path, 'c-1', '', 1.7e9, ('a', 'b'), ('a_cpu',)
    )


def test_task_round_trips_through_dict(tmp_path: Path) -> None:
    """Task round trips through dict."""
    task = state.Task(tmp_path, 'c', 'why slow?', 12.5, ('a',), ())

    assert state.Task.from_dict(task.to_dict()) == task


def test_trajectory_appends_and_reads_back(tmp_path: Path) -> None:
    """Trajectory appends and reads back."""
    path = tmp_path / 'run' / 'trajectory.jsonl'
    record = trajectory.Trajectory(path, 'run-1')
    record.record('node', node='orient')
    record.record('tool_call', role='tester', call_id='call_0001')

    events = trajectory.read_trajectory(path)

    assert [(e['seq'], e['type'], e['role']) for e in events] == [
        (1, 'node', 'agent'),
        (2, 'tool_call', 'tester'),
    ]


def test_trajectory_reopen_continues_sequence(tmp_path: Path) -> None:
    """Trajectory reopen continues sequence."""
    path = tmp_path / 'trajectory.jsonl'
    trajectory.Trajectory(path, 'run-1').record('node', node='orient')

    event = trajectory.Trajectory(path, 'run-1', 1).record('continuation')

    assert (event['seq'], event['continuation']) == (2, 1)


def test_message_round_trips_through_dict() -> None:
    """Message round trips through dict."""
    message = contracts.Message(
        'assistant',
        'thinking',
        (contracts.ToolCall('t1', 'run_sql', '{"query": "SELECT 1"}'),),
        provider_fields={'signature': 'x'},
    )

    rebuilt = trajectory.message_from_dict(
        json.loads(json.dumps(trajectory.message_to_dict(message)))
    )

    assert rebuilt == message


def test_report_round_trips_through_dict() -> None:
    """Report round trips through dict."""
    evidence = report.EvidenceRef('call_0002', 'metrics', '623.25')
    original = report.Report(
        run_id='r',
        continuation=1,
        outcome='provisional',
        stop_reason='tool_call_limit',
        fault_type='cpu',
        ranks=('a', 'b'),
        causes=(
            report.RankedCause(
                1,
                'a',
                'a saturates',
                'H1',
                'unverified',
                'low',
                ('x',),
                (evidence,),
            ),
        ),
        actions=(report.Action('H1', 'check a', 'read_only', 'cpu falls'),),
        data_requests=(report.DataRequest('H1', 'a logs', 'a host', 'conf'),),
        dropped=('rank b: no evidence',),
        counters={'tool_calls': 3},
        prompt_hash='abc',
    )

    rebuilt = report.Report.from_dict(
        json.loads(json.dumps(original.to_dict()))
    )

    assert rebuilt == original
