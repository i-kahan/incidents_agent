"""Official evaluator contracts and hand-computed cluster statistics."""

import dataclasses
import json

import pytest

from incidents_agent.eval import rcaeval_bridge
from incidents_agent.eval import scoring
from incidents_agent.eval import stats


@pytest.fixture
def three_cases(tmp_path):
    """Three answers, allowing missing/empty/service/indicator variants."""
    path = tmp_path / 'answers.jsonl'
    rows = [
        {
            'case_id': f'c{i}',
            'dataset': 're2-ob',
            'service': 'checkout',
            'fault': 'cpu',
            'indicator': 'cpu',
            'answer_in_candidates': True,
            'indicator_in_candidates': True,
            'n_candidate_services': 2,
            'n_candidate_indicators': 2,
        }
        for i in range(3)
    ]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


@pytest.mark.integration
@pytest.mark.parametrize(
    'ranks,service,indicator',
    [
        (['checkout'], 1, 0),
        (['checkout_cpu'], 1, 1),
        ([], 0, 0),
    ],
)
def test_official_three_case_contract(
    tmp_path, three_cases, ranks, service, indicator
):
    """Official three case contract."""
    predictions = tmp_path / 'predictions.jsonl'
    rcaeval_bridge.write_predictions(
        predictions, {'c0': ranks, 'c1': []}, 'fixture'
    )

    result = scoring.score_subset(
        three_cases,
        predictions,
        ['c0', 'c1', 'c2'],
        subset_path=tmp_path / 'subset.jsonl',
        tasks_dir=tmp_path / 'blind',
    )

    assert isinstance(result.summary['service'], dict)
    assert result.summary['service']['AC@1'] == pytest.approx(service / 3)
    assert isinstance(result.summary['indicator'], dict)
    assert result.summary['indicator']['AC@1'] == pytest.approx(indicator / 3)
    assert result.cases[2].service_rank is None


@pytest.mark.integration
def test_subset_has_no_phantom_misses(tmp_path, three_cases):
    """Subset has no phantom misses."""
    predictions = tmp_path / 'p.jsonl'
    rcaeval_bridge.write_predictions(
        predictions, {'c1': ['checkout_cpu']}, 'fixture'
    )

    result = scoring.score_subset(
        three_cases,
        predictions,
        ['c1'],
        subset_path=tmp_path / 'subset.jsonl',
        tasks_dir=tmp_path / 'blind',
    )

    assert result.summary['n_cases'] == 1
    assert isinstance(result.summary['service'], dict)
    assert result.summary['service']['AC@1'] == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    'text',
    [
        '{"case_id":"other","ranks":[]}\n',
        '{"case_id":"c0","ranks":[]}\n{"case_id":"c0","ranks":[]}\n',
        '{"case_id":"c0","ranks":"checkout"}\n',
    ],
)
def test_invalid_predictions_rejected(tmp_path, three_cases, text):
    """Invalid predictions rejected."""
    path = tmp_path / 'p.jsonl'
    path.write_text(text)

    with pytest.raises(rcaeval_bridge.ScoringError):
        scoring.score_subset(
            three_cases,
            path,
            ['c0'],
            subset_path=tmp_path / 'subset.jsonl',
            tasks_dir=tmp_path / 'blind',
        )


@pytest.mark.integration
def test_target_projection_reaches_official_evidence(tmp_path, three_cases):
    """Target projection reaches official evidence."""
    trajectory = tmp_path / 'projected.jsonl'
    scoring.export_trajectory(
        [
            {
                'type': 'tool_call',
                'tool': 'run_sql',
                'call_id': 'call_1',
                'ok': True,
                'target_components': ['checkout_cpu', 'cart'],
            },
            {'type': 'final_report'},
            {'type': 'tool_call', 'ok': True, 'target_components': ['late']},
        ],
        trajectory,
    )
    prediction = tmp_path / 'p.jsonl'
    prediction.write_text(
        json.dumps(
            {
                'case_id': 'c0',
                'ranks': ['checkout'],
                'trajectory': trajectory.name,
            }
        )
        + '\n'
    )

    result = scoring.score_subset(
        three_cases,
        prediction,
        ['c0'],
        subset_path=tmp_path / 'subset.jsonl',
        tasks_dir=tmp_path / 'blind',
    )

    assert result.cases[0].inspected_root is True
    assert isinstance(result.summary['evidence'], dict)
    assert result.summary['evidence']['inspected_root'] == 1
    assert 'late' not in trajectory.read_text()
    assert json.loads(trajectory.read_text().splitlines()[0])['derived'] is True


@pytest.mark.integration
def test_failed_call_does_not_project_targets(tmp_path):
    """Failed call does not project targets."""
    path = tmp_path / 'projection.jsonl'

    scoring.export_trajectory(
        [{'type': 'tool_call', 'ok': False, 'target_components': ['checkout']}],
        path,
    )

    assert len(path.read_text().splitlines()) == 1


def case(
    case_id: str,
    service: str,
    rank: int | None = 1,
    indicator: str | None = 'cpu',
) -> scoring.CaseScore:
    """Construct one typed official row."""
    return scoring.CaseScore(
        case_id,
        service,
        'cpu',
        indicator,
        rank,
        rank,
        service if rank == 1 else 'wrong',
    )


def test_cluster_interval_retains_correlated_repeats():
    """Cluster interval retains correlated repeats."""
    cases = [
        case('a1', 'a'),
        case('a2', 'a'),
        case('b1', 'b', None),
        case('b2', 'b', None),
    ]

    result = stats.accuracy(cases, 'service', 1, draws=1000)

    assert result.value == 0.5
    assert (result.low, result.high) == (0, 1)
    assert result.n_groups == 2
    assert result == stats.accuracy(
        list(reversed(cases)), 'service', 1, draws=1000
    )


def test_indicator_eligibility_and_single_group():
    """Indicator eligibility and single group."""
    result = stats.accuracy(
        [case('a', 'a'), case('b', 'b', indicator=None)], 'indicator', 1
    )

    assert result.value == 1
    assert result.n_cases == 1
    assert result.low is None
    assert result.reason


def test_paired_identical_results_have_zero_interval():
    """Paired identical results have zero interval."""
    cases = [case('a', 'a'), case('b', 'b', None)]

    result = stats.paired_difference(cases, cases, 'service', 1, draws=100)

    assert (result.value, result.low, result.high) == (0, 0, 0)


def test_paired_different_cases_rejected():
    """Paired different cases rejected."""
    with pytest.raises(ValueError, match='identical'):
        stats.paired_difference(
            [case('a', 'a')], [case('b', 'b')], 'service', 1
        )


def test_mcnemar_exact_hand_computed():
    """Mcnemar exact hand computed."""
    first = [case('a', 'a'), case('b', 'b')]
    second = [dataclasses.replace(c, service_rank=None) for c in first]

    result = stats.mcnemar(first, second)

    assert result['p_value'] == 0.5
    assert result['wins'] == 2


def test_tied_baselines_choose_guarded():
    """Tied baselines choose guarded."""
    cases = [case('a', 'a')]

    assert stats.choose_baseline(cases, cases) == 'guarded_baro'


def test_agreement_empty_predictions_are_not_agreement():
    """Agreement empty predictions are not agreement."""
    empty = dataclasses.replace(case('a', 'a', None), top_service=None)

    result = stats.agreement([empty], [empty])

    assert result['agreement'] == 0
    assert result['agree_accuracy'] is None
    assert result['differ_accuracy'] == 0


def test_repeat_noise_uses_all_pairs():
    """Repeat noise uses all pairs."""
    yes, no = [case('a', 'a')], [case('a', 'a', None)]

    result = stats.noise([yes, yes, no], ['same'] * 3)

    assert result['mean_flip_rate'] == pytest.approx(2 / 3)
    assert result['accuracy_range'] == 1


def test_repeat_configuration_mismatch_rejected():
    """Repeat configuration mismatch rejected."""
    with pytest.raises(ValueError, match='configurations'):
        stats.noise([[case('a', 'a')]] * 2, ['one', 'two'])
