"""Tests for reading answers files and writing subsets."""

import json
import pathlib

import pytest

from incidents_agent.eval import answers


def test_load_answers_keeps_file_order(answers_file: pathlib.Path) -> None:
    """Answers come back in file order."""
    loaded = answers.load_answers(answers_file)

    assert [a.case_id for a in loaded[:3]] == ['case-01', 'case-02', 'case-03']
    assert len(loaded) == 18


def test_load_answers_with_duplicate_case_raises(
    tmp_path: pathlib.Path,
) -> None:
    """A repeated case id is rejected."""
    row = json.dumps({'case_id': 'x', 'service': 's', 'fault': 'cpu'})
    path = tmp_path / 'a.jsonl'
    path.write_text(f'{row}\n{row}\n', encoding='utf-8')

    with pytest.raises(answers.AnswersError, match='duplicate'):
        answers.load_answers(path)


def test_load_answers_with_missing_service_raises(
    tmp_path: pathlib.Path,
) -> None:
    """A record without a service is rejected."""
    path = tmp_path / 'a.jsonl'
    path.write_text('{"case_id": "x", "fault": "cpu"}\n', encoding='utf-8')

    with pytest.raises(answers.AnswersError, match='service'):
        answers.load_answers(path)


def test_subset_with_unknown_case_raises(answers_file: pathlib.Path) -> None:
    """Selecting a case with no answer is an error."""
    loaded = answers.load_answers(answers_file)

    with pytest.raises(answers.AnswersError, match='no answer'):
        answers.subset(loaded, ['case-01', 'missing'])


def test_write_answers_round_trips_raw_records(
    answers_file: pathlib.Path,
) -> None:
    """A written subset reads back unchanged."""
    loaded = answers.load_answers(answers_file)
    chosen = answers.subset(loaded, ['case-05', 'case-02'])
    out = answers_file.parent / 'tuning.answers.jsonl'

    answers.write_answers(chosen, out, answers_file.parent / 'toy')

    assert answers.load_answers(out) == chosen


def test_write_answers_inside_task_view_raises(
    answers_file: pathlib.Path,
) -> None:
    """Answers can never be written into the task view."""
    loaded = answers.load_answers(answers_file)
    tasks_dir = answers_file.parent / 'toy'

    with pytest.raises(answers.AnswersError, match='task view'):
        answers.write_answers(loaded, tasks_dir / 'leak.jsonl', tasks_dir)
