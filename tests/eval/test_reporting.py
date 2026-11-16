"""Persisted reference comparisons, repeat integrity and usage coverage."""

import dataclasses
import json

import pytest

from incidents_agent.eval import answers
from incidents_agent.eval import baseline_reference
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import split


def test_missing_usage_is_not_reported_as_zero():
    """A failed case remains counted without inventing cost measurements."""
    summary = reporting.summarize_usage(
        [
            {
                'usage': {
                    'cost_usd': 2,
                    'outcome': 'verified',
                    'stop_reason': 'submitted',
                }
            },
            {'stop_reason': 'execution_error'},
        ]
    )

    assert summary['n_cases'] == 2
    assert isinstance(summary['usage'], dict)
    assert summary['usage']['cost_usd']['n_measured'] == 1
    assert summary['usage']['cost_usd']['mean'] == 2
    assert summary['citation_validity'] is None


@pytest.fixture
def reference_case(tmp_path):
    """A frozen tuning reference with a matching one-case official answer."""
    partition = split.Split(
        'toy', 6, 1, 'hash', {'g1': ('a',), 'g2': ('b',)}, ('g1',), ('g2',)
    )
    split_path = tmp_path / 'split.json'
    runner.atomic_json(split_path, partition.to_json())
    predictions = tmp_path / 'guarded_baro.tuning.predictions.jsonl'
    predictions.write_text('{"case_id":"a","ranks":["checkout_cpu"]}\n')
    reference = tmp_path / 'reference.json'
    runner.atomic_json(
        reference,
        {
            'baseline': 'guarded_baro',
            'tuning_ids': ['a'],
            'split_sha256': answers.file_sha256(split_path),
            'predictions_sha256': answers.file_sha256(predictions),
        },
    )
    answer = tmp_path / 'answers.jsonl'
    answer.write_text(
        json.dumps(
            {
                'case_id': 'a',
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
        + '\n'
    )
    agent = scoring.ScoredRun(
        {},
        (scoring.CaseScore('a', 'checkout', 'cpu', 'cpu', 1, 1, 'checkout'),),
    )
    return agent, dict(
        reference=reference,
        baseline_runs=tmp_path,
        split_path=split_path,
        answers_path=answer,
        tasks_dir=tmp_path / 'tasks',
    )


@pytest.mark.integration
def test_saved_reference_produces_paired_agreement(tmp_path, reference_case):
    """The saved tuning winner is scored on precisely the agent's cases."""
    agent, kwargs = reference_case
    output = tmp_path / 'report'
    output.mkdir()

    result = baseline_reference.compare(output, agent, **kwargs)

    assert result['baseline'] == 'guarded_baro'
    assert isinstance(result['agreement'], dict)
    assert result['agreement']['agreement'] == 1
    assert (output / 'baseline-comparison.json').exists()


def test_reference_rejects_changed_tuning_predictions(tmp_path, reference_case):
    """A saved selection cannot silently refer to changed baseline outputs."""
    agent, kwargs = reference_case
    (tmp_path / 'guarded_baro.tuning.predictions.jsonl').write_text('changed')

    with pytest.raises(ValueError, match='changed after selection'):
        baseline_reference.compare(tmp_path, agent, **kwargs)


def test_missing_reference_is_explicitly_unavailable(tmp_path, reference_case):
    """Missing baseline experiments do not become zero-valued comparisons."""
    agent, kwargs = reference_case
    kwargs['reference'].unlink()

    assert (
        baseline_reference.compare(tmp_path, agent, **kwargs)['available']
        is False
    )


@pytest.fixture
def repeats(tmp_path):
    """Two runs with distinct repeat IDs and the same frozen case inputs."""
    paths = []
    for index in range(2):
        folder = tmp_path / str(index)
        runner.atomic_json(
            folder / 'manifest.json',
            {
                'frozen': {
                    'configuration_fingerprint': 'same',
                    'dry_run': True,
                    'repeat_id': str(index),
                    'cases': [
                        {
                            'case_id': 'a',
                            'files': {'metrics.csv': 'digest'},
                            'suite': 'rcaeval',
                        }
                    ],
                }
            },
        )
        case = scoring.CaseScore(
            'a',
            'checkout',
            'cpu',
            'cpu',
            1 if index else None,
            1 if index else None,
            'checkout' if index else 'cart',
        )
        runner.atomic_json(
            folder / 'case-scores.json', {'cases': [dataclasses.asdict(case)]}
        )
        paths.append(folder)
    return paths


def test_saved_repeats_measure_noise(tmp_path, repeats):
    """One flipped case across two repeats gives a flip rate of one."""
    result = reporting.repeat_report(repeats, tmp_path / 'noise.json')

    assert result['mean_flip_rate'] == 1
    assert result['accuracy_range'] == 1


def test_cached_duplicates_cannot_masquerade_as_repeats(tmp_path, repeats):
    """Re-reading one cache entry is not a noise measurement."""
    with pytest.raises(ValueError, match='distinct repeat IDs'):
        reporting.repeat_report(
            [repeats[0], repeats[0]], tmp_path / 'noise.json'
        )


def test_changed_input_cannot_masquerade_as_repeat(tmp_path, repeats):
    """Repeat inputs must match even when configuration and case IDs match."""
    path = repeats[1] / 'manifest.json'
    value = json.loads(path.read_text())
    value['frozen']['cases'][0]['files']['metrics.csv'] = 'changed'
    runner.atomic_json(path, value)

    with pytest.raises(ValueError, match='configurations'):
        reporting.repeat_report(repeats, tmp_path / 'noise.json')
