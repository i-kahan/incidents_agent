"""Verify RCAEval's export, BARO ranking, and scoring with synthetic data."""

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
def test_baro_smoke_with_synthetic_shift_scores_expected_service(
    tmp_path: pathlib.Path,
) -> None:
    """BARO ranks checkout first and the evaluator scores one prediction."""
    report = benchmark_smoke.run_smoke(tmp_path)

    assert report['n_cases'] == report['n_predicted'] == 1
    assert isinstance(report['service'], dict)
    assert report['service']['AC@1'] == 1.0
