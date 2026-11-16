"""Deterministic summaries, one-call execution and settled-response recovery."""

import dataclasses
import json
from decimal import Decimal

import pytest

from incidents_agent.agent import config
from incidents_agent.eval import agent_adapter
from incidents_agent.eval import runner
from incidents_agent.eval import single_call
from incidents_agent.llm import contracts
from incidents_agent.llm import fake


@pytest.fixture
def blind_case(tmp_path):
    """Build metrics and log inputs with no answer file in the task view."""
    folder = tmp_path / 'case'
    folder.mkdir()
    (folder / 'metrics.csv').write_text(
        'time,checkout_cpu,cart_cpu\n'
        + ''.join(
            f'{1700000000 + i * 10},{10 + i % 3 + 100 * (i > 30)},{5 + i % 3}\n'
            for i in range(60)
        )
    )
    (folder / 'logs.csv').write_text(
        'time,service,message\n'
        + ''.join(
            f'{1700000000 + i * 10},checkout,'
            f'{"healthy" if i <= 30 else "checkout failed"}\n'
            for i in range(60)
        )
    )
    runner.atomic_json(
        folder / 'task.json',
        {
            'case_id': 'toy',
            'inject_time': 1700000300,
            'candidate_services': ['checkout', 'cart'],
            'candidate_indicators': ['checkout_cpu', 'cart_cpu'],
        },
    )
    return runner.Case('toy', folder)


def test_summary_is_deterministic_and_contains_new_logs(tmp_path, blind_case):
    """Preprocessing is byte-stable and excludes old patterns."""
    task = agent_adapter.task_for(blind_case)
    first, calls = single_call.build_input(task, tmp_path / 'db1')
    second, _ = single_call.build_input(task, tmp_path / 'db2')

    assert single_call.encode(first) == single_call.encode(second)
    assert isinstance(first['evidence'], dict)
    evidence = first['evidence']['content']
    assert evidence['metrics'][0]['column'] == 'checkout_cpu'
    assert evidence['logs'][0]['sample_text'] == 'checkout failed'
    assert evidence['logs'][0]['count_before'] == 0
    assert evidence['logs'][0]['components'] == ['checkout']
    assert calls == 4
    assert 'answers' not in single_call.encode(first)


def test_task_adapter_does_not_pass_answer_path(tmp_path, blind_case):
    """Only blind task fields are transferred to the agent task."""
    task_path = blind_case.folder / 'task.json'
    answer_path = tmp_path / 'answers.jsonl'
    data = runner.read_object(task_path)
    data['answers_path'] = str(answer_path)
    runner.atomic_json(task_path, data)

    task = agent_adapter.task_for(blind_case)

    assert str(answer_path) not in json.dumps(task.to_dict())


def test_new_pattern_is_selected_before_frequency_cutoff(tmp_path, blind_case):
    """Frequent pre-existing patterns cannot displace a new rare pattern."""
    (blind_case.folder / 'logs.csv').write_text(
        'time,service,message\n'
        + ''.join(f'1700000000,cart,old-{chr(65 + i)}\n' * 3 for i in range(30))
        + '1700000400,checkout,rare-new\n'
    )
    payload, _ = single_call.build_input(
        agent_adapter.task_for(blind_case), tmp_path / 'db'
    )

    assert isinstance(payload['evidence'], dict)
    assert (
        payload['evidence']['content']['logs'][0]['sample_text'] == 'rare-new'
    )


@pytest.mark.parametrize(
    'text',
    [
        'not json',
        '{"ranks":["foreign"]}',
        '{"ranks":["cart","cart"]}',
        '{"ranks":"cart"}',
        '{"ranks":[7]}',
        '{"ranks":[],"extra":1}',
        '[]',
    ],
)
def test_invalid_model_response_rejected(text, blind_case):
    """Malformed and out-of-candidate responses cannot enter predictions."""
    with pytest.raises(ValueError):
        single_call.parse_ranks(
            contracts.Message('assistant', text),
            agent_adapter.task_for(blind_case),
        )


@pytest.mark.parametrize('ranks', [[], ['checkout'], ['checkout_cpu', 'cart']])
def test_valid_rankings_preserved(ranks, blind_case):
    """Service, indicator and empty rankings retain their semantics."""
    assert (
        single_call.parse_ranks(
            contracts.Message('assistant', json.dumps({'ranks': ranks})),
            agent_adapter.task_for(blind_case),
        )
        == ranks
    )


def test_tool_request_rejected(blind_case):
    """A provider returning tool calls cannot activate a tool loop."""
    message = contracts.Message(
        'assistant', '{"ranks":[]}', (contracts.ToolCall('x', 'run_sql', '{}'),)
    )
    with pytest.raises(ValueError, match='without tool calls'):
        single_call.parse_ranks(message, agent_adapter.task_for(blind_case))


def test_missing_onset_rejected_before_model(blind_case, tmp_path):
    """The baseline never infers a split from labels or another model call."""
    task = dataclasses.replace(
        agent_adapter.task_for(blind_case), fault_start_s=None
    )
    with pytest.raises(ValueError, match='fault start'):
        single_call.build_input(task, tmp_path / 'db')


def test_missing_logs_explicit(tmp_path, blind_case):
    """Metric-only inputs still produce a usable bounded summary."""
    (blind_case.folder / 'logs.csv').unlink()
    payload, _ = single_call.build_input(
        agent_adapter.task_for(blind_case), tmp_path / 'db'
    )

    assert isinstance(payload['evidence'], dict)
    assert payload['evidence']['content']['logs'] == []
    assert isinstance(payload['evidence'], dict)
    assert payload['evidence']['content']['logs_available'] is False


def test_summary_is_bounded_and_redacted(tmp_path, blind_case):
    """Large new patterns cannot grow the evidence beyond its fixed budget."""
    (blind_case.folder / 'logs.csv').write_text(
        'time,service,message\n'
        + ''.join(
            f'1700000400,checkout,{chr(65 + i)}'
            + 'a' * 2000
            + ' password=supersecretvalue\n'
            for i in range(26)
        )
    )
    payload, _ = single_call.build_input(
        agent_adapter.task_for(blind_case), tmp_path / 'db'
    )
    assert isinstance(payload['evidence'], dict)
    evidence = payload['evidence']['content']

    assert len(single_call.encode(evidence).encode()) <= 16384
    assert evidence['truncated'] is True
    assert 'supersecretvalue' not in single_call.encode(payload)
    assert all(len(row['sample_text']) <= 320 for row in evidence['logs'])


@pytest.mark.integration
def test_single_completion_and_durable_recovery(tmp_path, blind_case):
    """A settled response can be recovered without a second request."""
    scripted = fake.ScriptedClient(
        [contracts.Message('assistant', '{"ranks":["checkout_cpu"]}')]
    )
    executor = single_call.Executor(lambda _: scripted, config.AgentConfig())
    first = executor(blind_case, tmp_path / 'attempts', 'r')
    recovered = executor(blind_case, tmp_path / 'attempts', 'r')

    assert len(scripted.requests) == 1
    assert scripted.requests[0][0].tools == ()
    assert recovered == first
    assert isinstance(first['usage'], dict)
    assert first['usage']['tool_calls'] == 0
    assert first['usage']['model_turns'] == 1
    assert first['usage']['cost_usd'] == 0


@pytest.mark.integration
def test_bad_response_retried_once_and_usage_aggregated(tmp_path, blind_case):
    """Malformed settled responses consume one retry, including its cost."""
    response = contracts.ChatResult(
        contracts.ProviderResult(
            contracts.Message('assistant', 'bad'),
            contracts.Usage(20, 4),
            'fake',
        ),
        'r',
        'now',
        'fake',
        Decimal('0.01'),
        Decimal('0.02'),
    )
    scripted = fake.ScriptedClient(
        [
            response,
            dataclasses.replace(
                response,
                response=dataclasses.replace(
                    response.response,
                    message=contracts.Message(
                        'assistant', '{"ranks":["checkout"]}'
                    ),
                ),
            ),
        ]
    )
    executor = single_call.Executor(lambda _: scripted, config.AgentConfig())
    frozen = runner.freeze([blind_case], {}, commit='test', dry_run=True)
    _, records = runner.run(
        [blind_case], frozen, output_root=tmp_path / 'out', execute=executor
    )

    assert len(scripted.requests) == 2
    assert scripted.requests[0][0] == scripted.requests[1][0]
    assert records[0]['ranks'] == ['checkout']
    assert isinstance(records[0]['usage'], dict)
    assert records[0]['usage']['cost_usd'] == pytest.approx(0.02)
    assert records[0]['usage']['input_tokens'] == 40
    assert records[0]['usage']['model_turns'] == 2


def test_inspection_ignores_candidates_and_discarded_evidence(blind_case):
    """Only included observations count as inspecting a service."""
    task = agent_adapter.task_for(blind_case)
    payload: dict[str, object] = {
        'candidate_services': ['cart', 'checkout'],
        'evidence': {
            'content': {'metrics': [{'column': 'checkout_cpu'}], 'logs': []},
        },
    }
    projected = single_call.inspection(payload, task)

    assert projected[0]['target_components'] == ['checkout']
