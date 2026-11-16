"""Tests for the pair-grouped tuning and held-out split."""

import json
import pathlib

import pytest

from incidents_agent.eval import answers
from incidents_agent.eval import split

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REAL_ANSWERS = _ROOT / 'agent_tasks' / 're2-ob.answers.jsonl'
_REAL_SPLIT = _ROOT / 'splits' / 're2-ob.json'


@pytest.fixture
def loaded(answers_file: pathlib.Path) -> list[answers.Answer]:
    """The toy answers, loaded."""
    return answers.load_answers(answers_file)


@pytest.fixture
def toy_split(loaded: list[answers.Answer]) -> split.Split:
    """A split of the toy answers with three tuning groups."""
    return split.make_split(
        loaded, dataset='toy', seed=7, n_tuning=3, answers_sha256='abc'
    )


def test_group_pairs_puts_each_pair_in_one_group(
    loaded: list[answers.Answer],
) -> None:
    """Repetitions of a pair share an opaque group id."""
    groups = split.group_pairs(loaded)

    assert [g.case_ids for g in groups[:2]] == [
        ('case-01', 'case-07', 'case-13'),
        ('case-02', 'case-08', 'case-14'),
    ]
    assert [g.group_id for g in groups] == [f'g0{n}' for n in range(1, 7)]


def test_group_pairs_with_uneven_repetitions_raises(
    loaded: list[answers.Answer],
) -> None:
    """A pair with a missing repetition is rejected."""
    with pytest.raises(split.SplitError, match='3 cases'):
        split.group_pairs(loaded[:-1])


def test_make_split_with_same_seed_is_deterministic(
    loaded: list[answers.Answer], toy_split: split.Split
) -> None:
    """The same seed gives the same split."""
    again = split.make_split(
        loaded, dataset='toy', seed=7, n_tuning=3, answers_sha256='abc'
    )

    assert again == toy_split


def test_make_split_sides_are_disjoint_and_complete(
    toy_split: split.Split,
) -> None:
    """Every case is on exactly one side."""
    tuning, heldout = set(toy_split.tuning), set(toy_split.heldout)

    assert tuning.isdisjoint(heldout)
    assert len(tuning | heldout) == 18


def test_make_split_has_requested_sizes(toy_split: split.Split) -> None:
    """Each side has the requested number of groups."""
    assert (len(toy_split.tuning_groups), len(toy_split.tuning)) == (3, 9)
    assert (len(toy_split.heldout_groups), len(toy_split.heldout)) == (3, 9)


def test_make_split_puts_no_pair_on_both_sides(
    loaded: list[answers.Answer], toy_split: split.Split
) -> None:
    """No service-fault pair is split across sides."""
    tuning = set(toy_split.tuning)
    pairs = {(a.service, a.fault): a.case_id in tuning for a in loaded}
    sides = {(a.service, a.fault, a.case_id in tuning) for a in loaded}

    assert len(sides) == len(pairs)


def test_make_split_tuning_covers_every_service_and_fault(
    loaded: list[answers.Answer], toy_split: split.Split
) -> None:
    """Tuning contains every service and fault type."""
    tuning = [a for a in loaded if a.case_id in set(toy_split.tuning)]

    assert {a.service for a in tuning} == {'svc-a', 'svc-b', 'svc-c'}
    assert {a.fault for a in tuning} == {'cpu', 'mem'}


def test_choose_tuning_when_coverage_impossible_raises(
    loaded: list[answers.Answer],
) -> None:
    """Too few groups to cover everything is an error."""
    groups = split.group_pairs(loaded)

    with pytest.raises(split.SplitError, match='covered'):
        split.choose_tuning(groups, seed=1, n_tuning=2)


@pytest.mark.parametrize('n_tuning', [0, 6], ids=['none', 'all'])
def test_choose_tuning_with_size_out_of_range_raises(
    loaded: list[answers.Answer], n_tuning: int
) -> None:
    """Tuning must be neither empty nor everything."""
    groups = split.group_pairs(loaded)

    with pytest.raises(split.SplitError, match='between'):
        split.choose_tuning(groups, seed=1, n_tuning=n_tuning)


def test_to_json_records_no_labels(toy_split: split.Split) -> None:
    """The written split names no service or fault."""
    text = json.dumps(toy_split.to_json())

    assert 'svc-' not in text
    assert 'cpu' not in text


def test_from_json_round_trips(toy_split: split.Split) -> None:
    """A split survives writing and reading."""
    record = json.loads(json.dumps(toy_split.to_json()))

    assert split.from_json(record) == toy_split


def test_from_json_with_group_on_both_sides_raises(
    toy_split: split.Split,
) -> None:
    """A group assigned twice is rejected."""
    record = toy_split.to_json()
    record['heldout_groups'] = list(toy_split.groups)

    with pytest.raises(split.SplitError, match='exactly one side'):
        split.from_json(record)


def test_from_json_with_edited_case_list_raises(
    toy_split: split.Split,
) -> None:
    """Case lists must match the group assignment."""
    record = toy_split.to_json()
    record['tuning'] = list(toy_split.tuning)[1:]

    with pytest.raises(split.SplitError, match='do not match'):
        split.from_json(record)


def test_verify_with_changed_answers_file_raises(
    loaded: list[answers.Answer], toy_split: split.Split
) -> None:
    """A different answers file fails verification."""
    with pytest.raises(split.SplitError, match='differs'):
        split.verify(toy_split, loaded, answers_sha256='other')


def test_verify_with_tampered_assignment_raises(
    loaded: list[answers.Answer], toy_split: split.Split
) -> None:
    """An assignment the seed did not produce fails."""
    swapped = split.Split(
        dataset=toy_split.dataset,
        seed=toy_split.seed,
        attempt=toy_split.attempt,
        answers_sha256=toy_split.answers_sha256,
        groups=toy_split.groups,
        tuning_groups=toy_split.heldout_groups,
        heldout_groups=toy_split.tuning_groups,
    )

    with pytest.raises(split.SplitError, match='gives a change'):
        split.verify(swapped, loaded, answers_sha256='abc')


@pytest.mark.skipif(
    not (_REAL_ANSWERS.is_file() and _REAL_SPLIT.is_file()),
    reason='needs the RE2-OB export (make export) and the committed split',
)
def test_committed_split_verifies_against_real_answers() -> None:
    """The committed RE2-OB split matches the export."""
    record = json.loads(_REAL_SPLIT.read_text(encoding='utf-8'))
    stored = split.from_json(record)
    real = answers.load_answers(_REAL_ANSWERS)

    split.verify(stored, real, answers.file_sha256(_REAL_ANSWERS))

    assert (len(stored.tuning), len(stored.heldout)) == (30, 60)
