"""Integration tests for the RCAEval baselines and evaluator wrappers."""

import csv
import json
import math
import pathlib

import pytest

from incidents_agent.eval import rcaeval_bridge
from scripts import benchmark_smoke

INJECT = 1_700_000_600
START, END = INJECT - 600, INJECT + 600


def _csv(
    path: pathlib.Path, header: list[str], rows: list[list[float]]
) -> None:
    lines = [','.join(header)] + [','.join(str(v) for v in r) for r in rows]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _wiggle(i: int) -> float:
    return 1.0 + 0.1 * math.sin(i)


@pytest.fixture
def multisource_case(tmp_path: pathlib.Path) -> pathlib.Path:
    """A blind RE2-style case where `checkout` shifts in every source."""
    case = tmp_path / 'case'
    case.mkdir()
    task = {
        'case_id': 'toy-0001',
        'dataset': 're2-ob',
        'inject_time': INJECT,
        'standard_window': {'start': START, 'end': END},
        'files': {
            'metrics': 'metrics.csv',
            'logts': 'logts.csv',
            'tracets_err': 'tracets_err.csv',
            'tracets_lat': 'tracets_lat.csv',
        },
        'candidate_services': ['cart', 'checkout'],
        'candidate_indicators': ['cart_cpu', 'checkout_cpu'],
    }
    (case / 'task.json').write_text(json.dumps(task), encoding='utf-8')
    seconds = range(START, END)
    _csv(
        case / 'metrics.csv',
        ['time', 'cart_cpu', 'checkout_cpu'],
        [[t, _wiggle(t), _wiggle(t) + 5 * (t >= INJECT)] for t in seconds],
    )
    steps = range(START, END, 15)
    for name, header in (
        ('logts.csv', ['time', 'cart_1', 'checkout_2']),
        ('tracets_err.csv', ['time', 'cart_GetCart', 'checkout_PlaceOrder']),
        ('tracets_lat.csv', ['time', 'cart_GetCart', 'checkout_PlaceOrder']),
    ):
        _csv(
            case / name,
            header,
            [[t, _wiggle(t), _wiggle(t) + 50 * (t >= INJECT)] for t in steps],
        )
    return case


@pytest.fixture
def answers_and_predictions(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Two answers with one correct and one wrong prediction."""
    answers = [
        {
            'case_id': f'x-{n}',
            'dataset': 're2-ob',
            'service': 'checkout',
            'fault': 'cpu',
            'indicator': 'cpu',
            'answer_in_candidates': True,
            'indicator_in_candidates': True,
            'n_candidate_services': 2,
            'n_candidate_indicators': 2,
        }
        for n in (1, 2)
    ]
    answers_path = tmp_path / 'answers.jsonl'
    answers_path.write_text(
        ''.join(json.dumps(a) + '\n' for a in answers), encoding='utf-8'
    )
    predictions_path = tmp_path / 'predictions.jsonl'
    rcaeval_bridge.write_predictions(
        predictions_path,
        {'x-1': ['checkout_cpu'], 'x-2': ['cart_cpu', 'checkout_cpu']},
        'toy',
    )
    return answers_path, predictions_path


@pytest.mark.integration
def test_baro_ranks_on_synthetic_case_puts_checkout_first(
    tmp_path: pathlib.Path,
) -> None:
    """BARO ranks the shifted checkout metric first."""
    tasks = benchmark_smoke.export_fixture(tmp_path)
    (case_dir,) = (tasks / 'cases').iterdir()

    ranks = rcaeval_bridge.baro_ranks(case_dir)

    assert ranks[0] == 'checkout_cpu'


@pytest.mark.integration
def test_mmbaro_ranks_on_multisource_case_puts_checkout_first(
    multisource_case: pathlib.Path,
) -> None:
    """Multi-source BARO ranks a checkout series first."""
    ranks = rcaeval_bridge.mmbaro_ranks(multisource_case)

    assert ranks[0].startswith('checkout_')


def test_candidate_services_reads_task(multisource_case: pathlib.Path) -> None:
    """Candidate services come from `task.json`, lower-cased."""
    services = rcaeval_bridge.candidate_services(multisource_case)

    assert services == frozenset({'cart', 'checkout'})


def test_keep_candidates_drops_unknown_services() -> None:
    """Names whose service is not a candidate are removed, order kept."""
    ranks = ['traceservice_Export', 'checkout_PlaceOrder', 'cart_1']

    kept = rcaeval_bridge.keep_candidates(ranks, {'cart', 'checkout'})

    assert kept == ['checkout_PlaceOrder', 'cart_1']


@pytest.mark.integration
def test_score_full_returns_rows_and_summary(
    answers_and_predictions: tuple[pathlib.Path, pathlib.Path],
) -> None:
    """A full score returns per-case rows and accuracy."""
    answers, predictions = answers_and_predictions

    summary, rows = rcaeval_bridge.score(
        answers, predictions, expected_cases=2, aggregate_only=False
    )

    assert len(rows) == 2
    assert isinstance(summary['service'], dict)
    assert summary['service']['AC@1'] == 0.5


@pytest.mark.integration
def test_score_aggregate_only_hides_cases_and_faults(
    answers_and_predictions: tuple[pathlib.Path, pathlib.Path],
) -> None:
    """Held-out scoring returns headline numbers and nothing per case."""
    answers, predictions = answers_and_predictions

    summary, rows = rcaeval_bridge.score(
        answers, predictions, expected_cases=2, aggregate_only=True
    )

    assert rows == []
    assert set(summary) == set(rcaeval_bridge.AGGREGATE_KEYS)


@pytest.mark.integration
def test_score_with_wrong_expected_count_raises(
    answers_and_predictions: tuple[pathlib.Path, pathlib.Path],
) -> None:
    """Scoring stops if the evaluator saw a different number of cases."""
    answers, predictions = answers_and_predictions

    with pytest.raises(rcaeval_bridge.ScoringError, match='expected 3'):
        rcaeval_bridge.score(
            answers, predictions, expected_cases=3, aggregate_only=True
        )


@pytest.mark.integration
def test_guarded_baro_excludes_bursty_zero_iqr(multisource_case):
    """A mostly zero normal column cannot dominate a sustained shift."""
    path = multisource_case / 'metrics.csv'
    with path.open() as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        t = int(row['time'])
        row['redis_diskio'] = str(100000 if t >= INJECT else int(t % 100 == 0))
    with path.open('w') as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    ranks = rcaeval_bridge.guarded_baro_ranks(multisource_case)

    assert ranks[0] == 'checkout_cpu'
    assert 'redis_diskio' not in ranks
