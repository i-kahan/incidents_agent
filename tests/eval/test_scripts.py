"""Offline public evaluation commands, comparisons and failure reports."""

import csv
import dataclasses
import json
import subprocess
import sys

import pytest

from incidents_agent.eval import comparison
from incidents_agent.eval import failures
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import single_call


@pytest.mark.integration
@pytest.mark.parametrize('mode', ['single', 'single_call'])
def test_three_case_evaluation_and_resume(tmp_path, evaluation_options, mode):
    """Both modes traverse blind input, cache and official subset scoring."""
    options = dataclasses.replace(evaluation_options, agent=mode)
    prepared = service.prepare(options)
    result = service.execute(prepared)

    assert result.summary['n_cases'] == 3
    assert result.summary['n_predicted'] == 3
    assert result.scored is not None
    assert all(case.service_rank == 1 for case in result.scored.cases)

    def no_calls(*args):
        raise AssertionError('Cache must be reused')

    resumed = service.execute(
        dataclasses.replace(
            prepared,
            executor=no_calls,
            options=dataclasses.replace(options, resume=result.folder),
        )
    )
    assert resumed.records == result.records
    assert resumed.invocation_cost_usd == 0


@pytest.mark.integration
def test_compare_two_models_and_failure_report(
    tmp_path, evaluation_options, monkeypatch
):
    """Sequential cached models produce tables and offline case diagnostics."""
    calls = []
    real_execute = service.execute

    def tracked(prepared, **kwargs):
        calls.append(prepared.options.model)
        return real_execute(prepared, **kwargs)

    monkeypatch.setattr(service, 'execute', tracked)
    folder, table = comparison.run(
        evaluation_options,
        ['haiku', 'gpt-small'],
        results_root=tmp_path / 'comparison',
    )
    saved = runner.read_object(folder / 'comparison.json')
    model_data = saved['models']
    assert isinstance(model_data, dict)

    assert calls == ['haiku', 'gpt-small']
    assert 'guarded_baro' in table
    assert '95%' in table
    with (folder / 'comparison.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    assert [row['model'] for row in rows] == [
        'baro',
        'guarded_baro',
        'haiku',
        'gpt-small',
    ]
    assert rows[2]['tool_calls/case'] == '0.0'
    assert (
        model_data['haiku']['manifest']['cases']
        == model_data['gpt-small']['manifest']['cases']
    )
    run_path = tmp_path.__class__(model_data['haiku']['run'])
    target, _ = failures.run(run_path)
    report = runner.read_object(target / 'failure-report.json')
    assert report['n_cases'] == 3
    assert report['counts'] == {'correct_top1': 3}

    def no_model(task):
        raise AssertionError('Repeated comparison must not call a model')

    monkeypatch.setattr(single_call, 'fake_client', no_model)
    repeated, _ = comparison.run(
        evaluation_options,
        ['haiku', 'gpt-small'],
        results_root=tmp_path / 'comparison',
    )
    repeated_data = runner.read_object(repeated / 'comparison.json')['models']
    assert isinstance(repeated_data, dict)
    assert repeated_data['haiku']['invocation_cost_usd'] == 0


@pytest.mark.parametrize(
    'models', [[], ['haiku', 'haiku'], ['haiku', 'unknown']]
)
def test_invalid_models_fail_before_execution(
    evaluation_options, models, monkeypatch
):
    """Invalid aliases and duplicate arms cannot start partial comparisons."""

    def forbidden(*args):
        raise AssertionError('No evaluation should execute')

    monkeypatch.setattr(service, 'execute', forbidden)

    with pytest.raises(ValueError):
        comparison.run(evaluation_options, models)


def test_missing_baselines_fail_before_calls(evaluation_options, monkeypatch):
    """Comparison requires the full-tuning reference before model calls."""
    (
        evaluation_options.baseline_root
        / 'results/phase-02/reference_baseline.json'
    ).unlink()

    def forbidden(*args):
        raise AssertionError('No evaluation should execute')

    monkeypatch.setattr(service, 'execute', forbidden)

    with pytest.raises(ValueError, match='make baselines'):
        comparison.run(evaluation_options, ['haiku'])


@pytest.mark.parametrize('selector', ['heldout', 'all', 'a,held'])
def test_comparison_heldout_gate(evaluation_options, selector):
    """Every selection route gates resolved held-out IDs."""
    with pytest.raises(ValueError, match='--final'):
        comparison.run(
            dataclasses.replace(evaluation_options, cases=selector), ['haiku']
        )


def test_single_call_rejects_field(evaluation_options):
    """The baseline cannot accidentally claim field-set support."""
    with pytest.raises(ValueError, match='RCAEval only'):
        service.prepare(dataclasses.replace(evaluation_options, cases='field'))


def test_default_baseline_root_regression(evaluation_options, monkeypatch):
    """Omitting --baseline-root uses the common repository root."""
    monkeypatch.setattr(
        service, 'shared_root', lambda: evaluation_options.baseline_root
    )
    prepared = service.prepare(
        dataclasses.replace(evaluation_options, baseline_root=None, limit=1)
    )
    result = service.execute(prepared)

    assert (result.folder / 'baseline-comparison.json').exists()


def test_missing_answer_subset_fails_before_execution(evaluation_options):
    """All intended labels must exist before spending on predictions."""
    evaluation_options.answers.write_text('')
    with pytest.raises(ValueError, match='no answer'):
        service.prepare(evaluation_options)


def test_fingerprint_distinguishes_mode_and_model(evaluation_options):
    """Single-call and agent runs cannot reuse each other's predictions."""
    first = service.prepare(evaluation_options)
    second = service.prepare(
        dataclasses.replace(evaluation_options, agent='single')
    )
    # A model other than the configured default.
    third = service.prepare(
        dataclasses.replace(evaluation_options, model='haiku')
    )

    assert (
        len(
            {
                item.manifest['configuration_fingerprint']
                for item in (first, second, third)
            }
        )
        == 3
    )


def test_subset_cache_identity_is_stable(evaluation_options):
    """Selection size does not invalidate a shared case's execution key."""
    full = service.prepare(evaluation_options)
    subset = service.prepare(dataclasses.replace(evaluation_options, limit=1))

    assert isinstance(full.manifest['cases'], list)
    assert isinstance(subset.manifest['cases'], list)
    assert runner.cache_key(
        full.manifest, full.manifest['cases'][0]
    ) == runner.cache_key(subset.manifest, subset.manifest['cases'][0])


@pytest.mark.parametrize(
    'row,expected',
    [
        ({'stop_reason': 'budget_refused'}, 'execution_blocked'),
        ({'error': 'invalid_response'}, 'execution_error'),
        ({'service_rank': 1}, 'correct_top1'),
        ({'has_ranking': False}, 'no_ranking'),
        (
            {'has_ranking': True, 'inspected_root': False},
            'wrong_root_uninspected',
        ),
        ({'has_ranking': True, 'inspected_root': True}, 'wrong_root_inspected'),
        ({'has_ranking': True}, 'wrong_inspection_unknown'),
    ],
)
def test_failure_categories(row, expected):
    """Primary categories are deterministic and do not infer model reasoning."""
    assert failures.category(row) == expected


def test_unknown_forced_state_not_guessed():
    """Low confidence or a wrong rank alone does not prove forced output."""
    assert failures.forced({'ranks': ['wrong']}) is None


@pytest.mark.integration
def test_missing_saved_result_stays_in_failure_denominator(evaluation_options):
    """A missing cached record is reported as an execution failure."""
    result = service.execute(service.prepare(evaluation_options))
    runner.atomic_json(
        result.folder / 'results.json', {'cases': result.records[:2]}
    )
    folder, _ = failures.run(result.folder)

    report = runner.read_object(folder / 'failure-report.json')
    assert report['n_cases'] == 3
    assert isinstance(report['counts'], dict)
    assert report['counts']['execution_error'] == 1


@pytest.mark.integration
def test_heldout_failure_report_requires_final(evaluation_options):
    """A saved final run still requires explicit permission to expose cases."""
    result = service.execute(
        service.prepare(
            dataclasses.replace(evaluation_options, cases='heldout', final=True)
        )
    )

    with pytest.raises(ValueError, match='--final'):
        failures.run(result.folder)
    folder, _ = failures.run(result.folder, final=True)
    assert runner.read_object(folder / 'failure-report.json')['n_cases'] == 1


@pytest.mark.integration
def test_public_cli_single_call(tmp_path, evaluation_options):
    """The public module accepts the single-call mode and scores it offline."""
    completed = subprocess.run(
        [
            sys.executable,
            '-m',
            'scripts.run_eval',
            '--agent',
            'single_call',
            '--dry-run',
            '--limit',
            '1',
            '--split',
            str(evaluation_options.split),
            '--tasks-root',
            str(evaluation_options.tasks_root),
            '--answers',
            str(evaluation_options.answers),
            '--baseline-root',
            str(tmp_path),
            '--output-root',
            str(evaluation_options.output_root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(completed.stdout)['summary']['n_cases'] == 1


def test_noise_runs_match_a_subset_and_keep_repeats_distinct(
    tmp_path, evaluation_options
):
    """Noise is attached only to an identical configuration and input subset."""
    options = dataclasses.replace(evaluation_options, model='haiku', limit=1)
    first = service.execute(
        service.prepare(dataclasses.replace(options, repeat_id='one'))
    )
    second = service.execute(
        service.prepare(dataclasses.replace(options, repeat_id='two'))
    )
    arm = service.prepare(dataclasses.replace(options, limit=None))
    result = comparison.matching_noise(
        [first.folder, second.folder], [arm], tmp_path / 'noise.json'
    )

    assert result['models'] == ['haiku']
    assert isinstance(result['measurements'], dict)
    assert result['measurements']['mean_flip_rate'] == 0
    with pytest.raises(ValueError, match='distinct repeat'):
        comparison.matching_noise(
            [first.folder, first.folder], [arm], tmp_path / 'noise.json'
        )
    other = service.prepare(dataclasses.replace(options, model='gpt-small'))
    with pytest.raises(ValueError, match='do not match'):
        comparison.matching_noise(
            [first.folder, second.folder], [other], tmp_path / 'noise.json'
        )


@pytest.mark.parametrize('mutation', ['duplicate', 'foreign'])
def test_baseline_predictions_validated_before_execution(
    evaluation_options, mutation
):
    """Invalid free rows cannot silently produce a biased comparison."""
    prepared = service.prepare(evaluation_options)
    path = (
        evaluation_options.baseline_root
        / 'runs/phase-02/baro.tuning.predictions.jsonl'
    )
    if mutation == 'duplicate':
        path.write_text(path.read_text() * 2)
    else:
        path.write_text('{"case_id":"foreign","ranks":[]}\n')

    with pytest.raises(ValueError):
        comparison.baseline_records('baro', prepared)


def test_missing_baseline_prediction_remains_a_miss(evaluation_options):
    """A missing prediction cannot shrink the intended comparison set."""
    prepared = service.prepare(evaluation_options)
    (
        evaluation_options.baseline_root
        / 'runs/phase-02/baro.tuning.predictions.jsonl'
    ).write_text('')
    records = comparison.baseline_records('baro', prepared)

    assert len(records) == 3
    assert all(row['ranks'] == [] for row in records)


@pytest.mark.parametrize('mutation', ['duplicate', 'foreign'])
def test_failure_report_rejects_invalid_records(evaluation_options, mutation):
    """Saved case identity corruption cannot be hidden by dictionary lookup."""
    result = service.execute(
        service.prepare(dataclasses.replace(evaluation_options, limit=1))
    )
    rows = (
        result.records * 2
        if mutation == 'duplicate'
        else [{'case_id': 'foreign'}]
    )
    runner.atomic_json(result.folder / 'results.json', {'cases': rows})

    with pytest.raises(ValueError):
        failures.run(result.folder)


def test_forced_uses_finalization_reasons_not_observation_quotes():
    """An untrusted quote cannot mark a normal submission as forced."""
    assert (
        failures.forced(
            {
                'report': {
                    'causes': [
                        {
                            'confidence_reasons': [
                                'forced finalisation: tool_call_limit'
                            ],
                        }
                    ]
                }
            }
        )
        is True
    )
    assert (
        failures.forced(
            {
                'report': {
                    'causes': [
                        {
                            'evidence': [
                                'forced finalisation: tool_call_limit'
                            ],
                        }
                    ]
                }
            }
        )
        is None
    )
