"""Real Phase 5 adapter boundaries and end-to-end offline evaluation."""

import json
import pathlib
import subprocess
import sys

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import fake_policy
from incidents_agent.agent import graph
from incidents_agent.agent import report
from incidents_agent.agent import state
from incidents_agent.eval import agent_adapter
from incidents_agent.eval import field
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.llm import contracts
from incidents_agent.llm import fake

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIELD = ROOT / 'evalsets/field'


def call(name: str, arguments: dict[str, object]) -> contracts.Message:
    """Build a scripted model tool request."""
    return contracts.Message(
        'assistant',
        tool_calls=(contracts.ToolCall(name, name, json.dumps(arguments)),),
    )


@pytest.mark.integration
@pytest.mark.parametrize('case_id', field.CASE_IDS)
def test_field_cases_run_through_upstream_graph_and_scoring(tmp_path, case_id):
    """The blind rule fake runs the graph and produces scoreable output."""
    case = runner.Case(case_id, FIELD / 'cases' / case_id, 'field')
    executor = agent_adapter.Executor(
        lambda task: fake_policy.RuleClient(), config.AgentConfig()
    )

    result = executor(case, tmp_path / 'attempts', 'fixture')
    raw = result['field_prediction']
    assert isinstance(raw, dict)
    prediction = agent_adapter.restore_prediction(raw)
    score = field.score(
        field.load_gold(FIELD / 'gold' / f'{case_id}.json'), prediction
    )

    assert score['case_id'] == case_id
    assert isinstance(result['usage'], dict)
    assert result['usage']['cost_usd'] == 0
    assert pathlib.Path(str(result['canonical_trajectory'])).exists()
    assert (
        'accepted_aliases'
        not in (tmp_path / 'attempts/fixture/manifest.json').read_text()
    )


@pytest.mark.integration
def test_scripted_field_report_validates_source_bound_citations(tmp_path):
    """Real tools support a scripted diagnosis and its citations."""
    task = state.Task(FIELD / 'cases/config_diff', case_id='config_diff')
    evidence = [
        {
            'call_id': 'call_0001',
            'locator': 'deploy_diff.patch::chunk_0000',
            'quote': 'AUTH_JWKS_TIMEOUT_MS',
        },
        {
            'call_id': 'call_0002',
            'locator': 'app',
            'quote': 'JWKS fetch timed out',
        },
        {
            'call_id': 'call_0003',
            'locator': 'runbook.md::chunk_0000',
            'quote': 'below 50ms',
        },
    ]
    client = fake.ScriptedClient(
        [
            call(
                'read_document', {'chunk_id': 'deploy_diff.patch::chunk_0000'}
            ),
            call(
                'search_text',
                {'table': 'app', 'pattern': 'JWKS fetch timed out'},
            ),
            call('read_document', {'chunk_id': 'runbook.md::chunk_0000'}),
            call(
                'update_hypotheses',
                {
                    'operations': [
                        {
                            'op': 'add',
                            'component': 'auth-service',
                            'statement': 'Timeout lowered',
                            'evidence': evidence,
                        }
                    ]
                },
            ),
            call(
                'submit',
                {
                    'outcome': 'diagnosis',
                    'ranks': ['auth-service'],
                    'fault_type': 'bad_config_change',
                },
            ),
        ]
    )
    result = graph.run_investigation(
        task,
        client,
        config.AgentConfig(verify=False),
        runs_dir=tmp_path,
        run_id='scripted',
    )
    checkpoint = json.loads((tmp_path / 'scripted/checkpoint.json').read_text())

    prediction = agent_adapter.field_prediction(
        result.report, result.events, checkpoint['catalog']
    )
    score = field.score(
        field.load_gold(FIELD / 'gold/config_diff.json'), prediction
    )

    assert score['passed'] is True
    assert len(prediction.citations) == 3


@pytest.mark.integration
def test_completed_report_recovery_never_calls_client(tmp_path):
    """Adapter recovery reads artifacts without rerunning a model."""
    case = runner.Case('healthy', FIELD / 'cases/healthy', 'field')
    first = agent_adapter.Executor(
        lambda task: fake_policy.RuleClient(), config.AgentConfig()
    )
    expected = first(case, tmp_path, 'recover')

    def forbidden(task):
        raise AssertionError('recovery called a model')

    recovered = agent_adapter.Executor(forbidden, config.AgentConfig())(
        case, tmp_path, 'recover'
    )

    assert recovered['report'] == expected['report']
    assert recovered['ranks'] == expected['ranks']


def final_report(run_id: str = 'r') -> report.Report:
    """Construct a minimal upstream report for citation-audit tests."""
    return report.Report(
        run_id,
        0,
        'inconclusive',
        'submitted',
        None,
        (),
        (),
        (),
        (),
        (),
        {},
        'hash',
    )


def test_attempted_invalid_citation_is_in_denominator():
    """Rejected evidence is counted even when no final claim survives."""
    events = [
        {
            'v': 1,
            'run_id': 'r',
            'type': 'model_call',
            'message': {
                'tool_calls': [
                    {
                        'arguments': json.dumps(
                            {
                                'evidence': [
                                    {
                                        'call_id': 'missing',
                                        'locator': 'x',
                                        'quote': 'fake',
                                    }
                                ]
                            }
                        )
                    }
                ]
            },
            'usage': {'input_tokens': 3, 'output_tokens': 1},
        }
    ]

    metrics = agent_adapter.trajectory_metrics(events, final_report())

    assert metrics['citation_attempts'] == 1
    assert metrics['citation_validity'] == 0
    assert metrics['input_tokens'] == 3


def test_adapter_rejects_unknown_trajectory_version():
    """An incompatible upstream format cannot silently produce metrics."""
    with pytest.raises(ValueError, match='version'):
        agent_adapter.trajectory_metrics([{'v': 99}], final_report())


def test_adapter_rejects_foreign_run():
    """Tool events from another run are never accepted as report evidence."""
    with pytest.raises(ValueError, match='run IDs'):
        agent_adapter.trajectory_metrics(
            [{'v': 1, 'run_id': 'other'}], final_report()
        )


@pytest.mark.integration
def test_three_benchmark_cases_export_and_score_offline(tmp_path):
    """Three fixtures traverse graph, cache, export and official scoring."""
    answers = []
    cases = []
    for index in range(3):
        case_id = f'toy-{index}'
        folder = tmp_path / 'tasks/cases' / case_id
        folder.mkdir(parents=True)
        (folder / 'metrics.csv').write_text(
            'time,checkout_cpu\n'
            + ''.join(
                f'{1700000000 + i * 10},{10 + i % 3 + 100 * (i >= 30)}\n'
                for i in range(60)
            )
        )
        (folder / 'task.json').write_text(
            json.dumps(
                {
                    'case_id': case_id,
                    'candidate_services': ['checkout'],
                    'candidate_indicators': ['checkout_cpu'],
                    'inject_time': 1700000300,
                }
            )
        )
        cases.append(runner.Case(case_id, folder))
        answers.append(
            {
                'case_id': case_id,
                'dataset': 're2-ob',
                'service': 'checkout',
                'fault': 'cpu',
                'indicator': 'cpu',
                'answer_in_candidates': True,
                'indicator_in_candidates': True,
                'n_candidate_services': 1,
                'n_candidate_indicators': 1,
            }
        )
    answers_path = tmp_path / 'answers.jsonl'
    answers_path.write_text(''.join(json.dumps(row) + '\n' for row in answers))
    frozen = runner.freeze(
        cases, {'model': 'fake'}, commit='test', dry_run=True
    )
    executor = agent_adapter.Executor(
        lambda task: fake_policy.RuleClient(task.candidate_services),
        config.AgentConfig(),
    )

    folder, records = runner.run(
        cases, frozen, output_root=tmp_path / 'out', execute=executor, workers=2
    )
    scores = reporting.benchmark_report(
        folder,
        records,
        answers_path=answers_path,
        tasks_dir=tmp_path / 'tasks',
        draws=100,
    )

    assert scores.summary['n_cases'] == 3
    assert scores.summary['n_predicted'] == 3
    assert all(case.service_rank == 1 for case in scores.cases)
    assert all(
        isinstance(record['usage'], dict) and record['usage']['cost_usd'] == 0
        for record in records
    )
    assert (folder / 'summary.json').exists()


@pytest.mark.integration
def test_cli_field_dry_run_writes_separate_table(tmp_path):
    """The public CLI executes seven cases and never emits benchmark scores."""
    completed = subprocess.run(
        [
            sys.executable,
            '-m',
            'scripts.run_eval',
            '--cases',
            'field',
            '--dry-run',
            '--output-root',
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(completed.stdout)
    folder = pathlib.Path(output['output'])

    assert output['summary']['n_scored'] == 7
    assert (folder / 'field-summary.md').exists()
    assert not (folder / 'predictions.jsonl').exists()
    assert not (tmp_path / 'real').exists()


@pytest.mark.integration
def test_upstream_billing_error_remains_a_single_failed_attempt(tmp_path):
    """Phase 5's captured billing error survives the evaluation adapter."""
    case = runner.Case('healthy', FIELD / 'cases/healthy', 'field')
    failure = fake.ScriptedClient([contracts.BillingUncertain('test')])
    executor = agent_adapter.Executor(lambda _: failure, config.AgentConfig())
    manifest = runner.freeze([case], {}, commit='test', dry_run=True)

    _, output = runner.run(
        [case], manifest, output_root=tmp_path, execute=executor
    )

    assert len(failure.requests) == 1
    assert output[0]['error'] == 'BillingUncertain'
    assert output[0]['ranks'] == []


@pytest.mark.parametrize(
    'tool,content',
    [
        ('run_sql', {'rows': [{'service': 'checkout'}]}),
        (
            'search_documents',
            {
                'results': [
                    {
                        'chunk_id': 'notes.md::chunk_0000',
                        'excerpt': 'checkout stalled',
                    },
                ]
            },
        ),
    ],
)
def test_inspection_projects_candidate_observed_in_returned_content(
    tool, content
):
    """Generic tool results contribute factual candidate targets."""
    event = {
        'type': 'tool_call',
        'run_id': 'r',
        'call_id': 'call_1',
        'tool': tool,
        'ok': True,
        'result': {'ok': True, 'untrusted_data': {'content': content}},
    }

    projected = agent_adapter.inspection_events([event], ['checkout', 'cart'])

    assert projected[0]['target_components'] == ['checkout']
    assert 'target_components' not in event
