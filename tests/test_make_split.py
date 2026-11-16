"""Tests for the split script: create once, then verify and never rewrite."""

import json
import pathlib

import pytest

from incidents_agent.eval import answers
from incidents_agent.eval import split
from scripts import make_split


@pytest.fixture
def toy_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tasks root with a toy export of six pairs over three services."""
    (tmp_path / 'toy').mkdir()
    services = ['svc-a', 'svc-b', 'svc-c']
    faults = ['cpu', 'mem']
    rows = [
        {
            'case_id': f'case-{n:02d}',
            'service': services[n % 3],
            'fault': faults[n % 2],
        }
        for n in range(18)
    ]
    text = ''.join(json.dumps(row) + '\n' for row in rows)
    (tmp_path / 'toy.answers.jsonl').write_text(text, encoding='utf-8')
    return tmp_path


@pytest.fixture
def small_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the script to three tuning groups for the toy export."""
    monkeypatch.setattr(make_split, 'N_TUNING_GROUPS', 3)


@pytest.mark.usefixtures('small_split')
def test_run_writes_subsets_beside_answers(toy_root: pathlib.Path) -> None:
    """Subsets land beside the answers, not in the view."""
    created = make_split.run('toy', toy_root, toy_root / 'splits')

    tuning = answers.load_answers(toy_root / 'tuning.answers.jsonl')
    heldout = answers.load_answers(toy_root / 'heldout.answers.jsonl')
    assert tuple(sorted(a.case_id for a in tuning)) == created.tuning
    assert tuple(sorted(a.case_id for a in heldout)) == created.heldout
    assert list((toy_root / 'toy').iterdir()) == []


@pytest.mark.usefixtures('small_split')
def test_run_second_time_leaves_split_file_unchanged(
    toy_root: pathlib.Path,
) -> None:
    """A second run verifies and does not rewrite."""
    splits_dir = toy_root / 'splits'
    make_split.run('toy', toy_root, splits_dir)
    before = (splits_dir / 'toy.json').read_bytes()

    make_split.run('toy', toy_root, splits_dir)

    assert (splits_dir / 'toy.json').read_bytes() == before


@pytest.mark.usefixtures('small_split')
def test_run_with_changed_answers_refuses(toy_root: pathlib.Path) -> None:
    """A changed answers file stops the script."""
    splits_dir = toy_root / 'splits'
    make_split.run('toy', toy_root, splits_dir)
    path = toy_root / 'toy.answers.jsonl'
    path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')

    with pytest.raises(split.SplitError, match='differs'):
        make_split.run('toy', toy_root, splits_dir)
