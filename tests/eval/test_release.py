"""Regression tests for Phase 10 admission and artifact validation."""

import dataclasses
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest

from incidents_agent.agent import config as agent_config
from incidents_agent.eval import field
from incidents_agent.eval import onset
from incidents_agent.eval import publication
from incidents_agent.eval import reference_agent
from incidents_agent.eval import release
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import split
from incidents_agent.llm import contracts
from incidents_agent.llm import fake
from incidents_agent.llm import ledger


def test_claim_rejects_second_writer(tmp_path):
    """A concurrent process cannot claim the same release."""
    with release.claim(tmp_path):
        with pytest.raises(BlockingIOError), release.claim(tmp_path):
            pytest.fail('Claim unexpectedly acquired twice')


def test_run_resumes_fixed_folder_after_interruption(tmp_path, monkeypatch):
    """An interrupted arm reuses the registered manifest and completes once."""
    root = tmp_path / 'phase-10'
    manifest: dict[str, object] = {'git_commit': 'frozen'}
    runner.atomic_json(
        root / 'release.json',
        {'arms': {'primary': manifest}, 'scoring_inputs': {}},
    )
    zero = Decimal(0)
    spend = SimpleNamespace(
        snapshot=lambda: ledger.Snapshot(zero, zero, zero, {}, False)
    )
    prepared = service.Prepared(
        service.Options(),
        (),
        manifest,
        tmp_path,
        tmp_path,
        tmp_path,
        tmp_path,
        lambda *args: {},
        mock.Mock(wraps=spend),
    )
    monkeypatch.setattr(release, 'prepare', lambda *_: prepared)
    monkeypatch.setattr(release, 'scoring_inputs', lambda _: {})
    folders = []

    def execute(arm, **_):
        folder = arm.options.resume
        folders.append(folder)
        assert (
            runner.read_object(folder / 'manifest.json')['frozen'] == manifest
        )
        if len(folders) == 1:
            raise OSError('interrupted')
        runner.atomic_json(folder / 'results.json', {'cases': []})
        return SimpleNamespace(invocation_cost_usd=0.02)

    monkeypatch.setattr(service, 'execute', execute)
    with pytest.raises(OSError, match='interrupted'):
        release.run_arm(root, tmp_path, 'primary')
    folder = release.run_arm(root, tmp_path, 'primary')
    assert release.run_arm(root, tmp_path, 'primary') == folder
    assert folders == [folder, folder]


def test_run_rejects_scoring_input_drift(tmp_path, monkeypatch):
    """Changes to scoring evidence stop the arm before calling its executor."""
    runner.atomic_json(
        tmp_path / 'release.json',
        {'arms': {'primary': {'git_commit': 'frozen'}}, 'scoring_inputs': {}},
    )
    prepared = SimpleNamespace(manifest={'git_commit': 'frozen'})
    monkeypatch.setattr(release, 'prepare', lambda *_: prepared)
    monkeypatch.setattr(release, 'scoring_inputs', lambda _: {'answers': 'new'})
    with pytest.raises(ValueError, match='inputs changed'):
        release.run_arm(tmp_path, tmp_path, 'primary')


@pytest.mark.parametrize(
    'ids',
    [{'a', 'b'}, {'z'}],
)
def test_aligned_rejects_incomplete_or_foreign_cases(ids):
    """Missing predictions cannot silently become empty rankings."""
    with pytest.raises(ValueError, match='frozen case IDs'):
        publication.aligned([{'case_id': 'a', 'ranks': []}], ids)


def test_model_summary_reports_drift_and_missing_attempts():
    """Coverage and model drift are separately observable."""
    summary = publication.model_summary(
        [
            {
                'attempts': [
                    {'requested_models': ['alias'], 'served_models': ['new']},
                    {'requested_models': ['alias'], 'served_models': []},
                ]
            }
        ],
        ['tuned'],
    )
    assert summary['matches_tuning'] is False
    assert summary['attempts_with_served_id'] == 1
    assert summary['attempts'] == 2


@pytest.mark.integration
def test_publish_scores_saved_runs_without_preparing_clients(
    tmp_path, monkeypatch
):
    """Publish three persisted arms using official scores and a moved bundle."""
    root = tmp_path / 'phase-10'
    tasks = tmp_path / 'agent_tasks/re2-ob'
    cases = []
    labels = []
    for number in range(60):
        case = f'test-{number:02d}'
        service_name = f'svc-{number % 4}'
        folder = tasks / 'cases' / case
        runner.atomic_json(
            folder / 'task.json',
            {'inject_time': 1700000000, 'candidate_services': [service_name]},
        )
        cases.append({'case_id': case, 'ranks': [f'{service_name}_cpu']})
        labels.append(
            {
                'case_id': case,
                'dataset': 're2-ob',
                'service': service_name,
                'fault': 'cpu',
                'indicator': 'cpu',
                'n_candidate_services': 1,
                'n_candidate_indicators': 1,
                'answer_in_candidates': True,
                'indicator_in_candidates': True,
            }
        )
    answers = tasks.parent / 'heldout.answers.jsonl'
    answers.write_text(''.join(json.dumps(row) + '\n' for row in labels))
    ids = [str(row['case_id']) for row in cases]
    for name in ('baro', 'guarded_baro'):
        path = tmp_path / f'runs/phase-02/{name}.heldout.predictions.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ''.join(
                json.dumps({'case_id': case, 'ranks': []}) + '\n'
                for case in ids
            )
        )
    partition = split.Split(
        're2-ob', 6, 1, 'toy', {'held': tuple(ids)}, (), ('held',)
    )
    manifests = {}
    for arm in release.REQUIRED:
        folder = tmp_path / 'runs/phase-06/real/runs' / f'phase-10-{arm}'
        if arm == 'field':
            rows = [{'case_id': case, 'ranks': []} for case in field.CASE_IDS]
        else:
            rows = cases
        manifest = {
            'configuration': {'split_hash': runner.digest(partition.to_json())},
            'partition': partition.to_json(),
            'heldout_ids': ids,
            'dry_run': False,
            'cases': [
                {
                    'case_id': row['case_id'],
                    'suite': 'rcaeval',
                    'files': runner.file_hashes(
                        tasks / 'cases' / str(row['case_id'])
                    )
                    if arm != 'field'
                    else {},
                }
                for row in rows
            ],
        }
        manifests[arm] = manifest
        runner.atomic_json(folder / 'manifest.json', {'frozen': manifest})
        runner.atomic_json(folder / 'results.json', {'cases': rows})
        if arm == 'primary':
            reporting.benchmark_report(
                folder, rows, answers_path=answers, tasks_dir=tasks, draws=5
            )
        runner.atomic_json(
            root / f'{arm}.complete.json',
            {'results_sha256': release.hash_file(folder / 'results.json')},
        )
    runner.atomic_json(
        root / 'release.json',
        {
            'commit': 'synthetic',
            'arms': manifests,
            'scoring_inputs': release.scoring_inputs(tmp_path),
            'expected_served_models': ['test'],
        },
    )

    def no_provider(*_args, **_kwargs):
        pytest.fail('Offline publication attempted paid preparation')

    monkeypatch.setattr(service, 'prepare', no_provider)
    monkeypatch.setattr(
        onset, 'evaluate', lambda inputs: {'n_cases': len(inputs)}
    )
    bundle = tmp_path / 'published'
    publication.publish(root, tmp_path, bundle)
    moved = bundle.rename(tmp_path / 'relocated')
    result = json.loads((moved / 'comparison.json').read_text())

    assert result['official']['primary']['service']['AC@1'] == 1
    assert result['official']['baro']['service']['AC@1'] == 0
    assert result['paired']['baro']['differences']['service_AC@1']['value'] == 1
    assert (moved / 'field/field-summary.md').is_file()
    assert (moved / 'paired-differences.csv').is_file()
    assert not list(moved.rglob('*answers*'))


@pytest.mark.integration
def test_reference_uses_injected_model_and_real_upstream_submit(
    tmp_path, evaluation_options
):
    """The reference arm delegates completion to the budget-aware interface."""
    case_dir = evaluation_options.tasks_root / 'cases/a'
    path = case_dir / 'task.json'
    task = runner.read_object(path)
    task.update(
        files=['metrics.csv'],
        system='test',
        standard_window={'start': 1700000000, 'end': 1700000400},
    )
    runner.atomic_json(path, task)
    scripted = fake.ScriptedClient(
        [
            contracts.Message(
                'assistant',
                tool_calls=(
                    contracts.ToolCall(
                        'submit-1', 'submit', '{"ranks":["svc-a_cpu"]}'
                    ),
                ),
            ),
        ]
    )
    executor = reference_agent.Executor(
        lambda _: scripted,
        agent_config.AgentConfig(model_alias='gpt-small'),
        None,
    )

    result = executor(runner.Case('a', case_dir), tmp_path, 'test-run')

    assert result['ranks'] == ['svc-a_cpu']
    assert len(scripted.requests) == 1
    request, run, case_id = scripted.requests[0]
    assert case_id == 'a'
    assert run == contracts.Run('test-run')
    assert 'submit' in {tool.name for tool in request.tools}


def test_legacy_paid_entry_point_refuses_heldout(evaluation_options):
    """The generic runner cannot start a replacement paid held-out run."""
    options = dataclasses.replace(
        evaluation_options, cases='heldout', dry_run=False, final=True
    )

    with pytest.raises(ValueError, match='require scripts.final_eval'):
        service.prepare(options)
