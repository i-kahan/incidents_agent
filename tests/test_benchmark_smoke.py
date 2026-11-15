"""Verify the RCAEval command-line contract with synthetic data."""

import json
import pathlib

import pytest

from scripts import benchmark_smoke


@pytest.mark.integration
def test_export_fixture_keeps_answers_outside_task_view(
    tmp_path: pathlib.Path,
) -> None:
    """A blind task directory exposes no answers file."""
    tasks = benchmark_smoke.export_fixture(tmp_path)

    assert tasks.with_suffix('.answers.jsonl').is_file()
    assert list(tasks.rglob('*answers*')) == []


@pytest.mark.integration
def test_score_fixture_with_matching_prediction_counts_one_case(
    tmp_path: pathlib.Path,
) -> None:
    """The matching synthetic prediction receives top-1 credit."""
    tasks = benchmark_smoke.export_fixture(tmp_path)
    manifest = json.loads((tasks / 'manifest.json').read_text(encoding='utf-8'))
    prediction = tmp_path / 'predictions.jsonl'
    line = {'case_id': manifest['case_ids'][0], 'ranks': ['checkout_cpu']}
    prediction.write_text(json.dumps(line) + '\n', encoding='utf-8')

    report = benchmark_smoke.score_fixture(tasks, prediction)

    assert report['n_cases'] == report['n_predicted'] == 1
    assert isinstance(report['service'], dict)
    assert report['service']['AC@1'] == 1.0
