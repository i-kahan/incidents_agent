"""Typed wrappers around RCAEval's baselines and its official evaluator.

This module needs the `benchmark` dependency group. RCAEval ships without
type information, so every value crossing this boundary is converted to a
plain typed value here.
"""

import importlib
import json
import pathlib
import typing
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence

# RCAEval 1.12.0 ships neither type stubs nor a py.typed marker.
import RCAEval.agent.predictions  # type: ignore[import-untyped]
import RCAEval.agent.prompt  # type: ignore[import-untyped]
import RCAEval.agent.toolkit  # type: ignore[import-untyped]
import RCAEval.benchmark.scoring  # type: ignore[import-untyped]

# `RCAEval.e2e` re-exports the `baro` function under the module's own name,
# so attribute access would find the function, not the module.
_BARO = importlib.import_module('RCAEval.e2e.baro')

AGGREGATE_KEYS = ('n_cases', 'n_predicted', 'service', 'indicator')


class ScoringError(ValueError):
    """The evaluator did not score the cases it was meant to."""


def _case(case_dir: pathlib.Path) -> typing.Any:
    return RCAEval.agent.toolkit.CaseToolkit(str(case_dir))


def baro_ranks(case_dir: pathlib.Path) -> list[str]:
    """Rank a case's metrics with BARO over the standard window.

    Unlike the toolkit's `rank_anomalies`, which keeps 20 entries, this
    returns BARO's full ranking, with RCAEval's default preprocessing.

    Args:
        case_dir: One case directory of a blind task view.

    Returns:
        Metric names, most anomalous first.
    """
    case = _case(case_dir)
    result = _BARO.baro(
        case.standard_metrics(),
        case.inject_time,
        dataset=case.task['dataset'],
    )
    return [str(name) for name in result['ranks']]


def mmbaro_ranks(case_dir: pathlib.Path) -> list[str]:
    """Rank metrics, log templates and trace series with multi-source BARO.

    Every source is cut to the case's standard window, the same window BARO
    and the toolkit use, and NaNs are filled as RCAEval's multi-source BARO
    expects.

    Args:
        case_dir: One case directory of a blind task view.

    Returns:
        Series names, most anomalous first.
    """
    case = _case(case_dir)
    start, end = case.window['start'], case.window['end']

    def cut(frame: typing.Any) -> typing.Any:
        return frame[(frame['time'] >= start) & (frame['time'] < end)]

    data = {
        'metric': cut(case.metrics()),
        'logts': cut(case.series('logts')),
        'tracets_err': cut(case.series('tracets_err')),
        'tracets_lat': cut(case.series('tracets_lat')),
    }
    result = _BARO.mmbaro(
        data, inject_time=case.inject_time, dataset=case.task['dataset']
    )
    return [str(name) for name in result['ranks']]


def candidate_services(case_dir: pathlib.Path) -> frozenset[str]:
    """Return the candidate services listed in a case's `task.json`."""
    task = json.loads((case_dir / 'task.json').read_text(encoding='utf-8'))
    return frozenset(str(s).lower() for s in task['candidate_services'])


def keep_candidates(ranks: Sequence[str], services: Iterable[str]) -> list[str]:
    """Drop ranked names whose service is not a candidate.

    The service of a name is found exactly as RCAEval's scorer finds it.

    Args:
        ranks: Ranked names.
        services: The case's candidate services.

    Returns:
        The names that map to a candidate service, in order.
    """
    allowed = set(services)
    return [
        name
        for name in ranks
        if RCAEval.benchmark.scoring.to_service(name.lower()) in allowed
    ]


def write_predictions(
    path: pathlib.Path, ranks_by_case: Mapping[str, Sequence[str]], model: str
) -> None:
    """Write a predictions file in RCAEval's format.

    Args:
        path: The output JSON lines file.
        ranks_by_case: Ranked names per case id.
        model: The value of each record's `model` field.
    """
    lines = [
        json.dumps({'case_id': case_id, 'ranks': list(ranks), 'model': model})
        + '\n'
        for case_id, ranks in ranks_by_case.items()
    ]
    path.write_text(''.join(lines), encoding='utf-8')


def score(
    answers_path: pathlib.Path,
    predictions_path: pathlib.Path,
    *,
    expected_cases: int,
    aggregate_only: bool,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Score predictions with RCAEval's evaluator, in process.

    Only the cases in the answers file are scored, so a subset must be
    scored against its own answers subset. With `aggregate_only`, the
    per-case rows and the per-fault breakdown are discarded before they are
    returned, so held-out results can be scored without being seen case by
    case.

    Args:
        answers_path: The answers file, or a subset of it.
        predictions_path: Predictions for those cases only; RCAEval
            rejects a line whose case is not in the answers file.
        expected_cases: How many cases the caller means to score.
        aggregate_only: Keep only the headline scores.

    Returns:
        The summary and the per-case rows (empty when `aggregate_only`).

    Raises:
        ScoringError: The evaluator scored a different number of cases.
    """
    answers = RCAEval.agent.predictions.load_answers(str(answers_path))
    case_ids = [a['case_id'] for a in answers]
    try:
        predictions = RCAEval.agent.predictions.load_predictions(
            str(predictions_path), case_ids
        )
    except RCAEval.agent.predictions.PredictionsError as error:
        raise ScoringError('; '.join(error.errors)) from error
    raw_summary, raw_rows = RCAEval.agent.predictions.evaluate(
        answers, predictions, predictions_path=str(predictions_path)
    )
    summary: dict[str, object] = dict(raw_summary)
    if summary['n_cases'] != expected_cases:
        raise ScoringError(
            f'scored {summary["n_cases"]} cases, expected {expected_cases}'
        )
    if aggregate_only:
        return {key: summary[key] for key in AGGREGATE_KEYS}, []
    rows: list[dict[str, object]] = [dict(row) for row in raw_rows]
    return summary, rows


def reference_prompt_chars(case_dir: pathlib.Path) -> int:
    """Characters RCAEval's reference agent sends before any tool result.

    That is its system and case prompt plus its tool schemas, which are
    sent on every turn.

    Args:
        case_dir: One case directory of a blind task view.

    Returns:
        The character count.
    """
    task = json.loads((case_dir / 'task.json').read_text(encoding='utf-8'))
    prompt = str(RCAEval.agent.prompt.full_prompt(task))
    tools = json.dumps(RCAEval.agent.toolkit.TOOLS)
    return len(prompt) + len(tools)


def tool_result_chars(
    case_dir: pathlib.Path, calls: Sequence[tuple[str, Mapping[str, object]]]
) -> list[int]:
    """Run reference-toolkit calls on a case and measure each result.

    Args:
        case_dir: One case directory of a blind task view.
        calls: Tool names with their arguments.

    Returns:
        The serialised size of each result, in call order.
    """
    case = _case(case_dir)
    for name, args in calls:
        case.call(name, dict(args))
    return [int(call['result_chars']) for call in case.calls]


def service_name(name: str) -> str:
    """Normalize a ranked name using the official service mapping."""
    return str(RCAEval.benchmark.scoring.to_service(name.strip().lower()))


def guarded_baro_ranks(case_dir: pathlib.Path) -> list[str]:
    """Run unchanged BARO after dropping zero-IQR normal-window metrics."""
    case = _case(case_dir)
    frame = case.standard_metrics()
    normal = frame[frame['time'] < case.inject_time]
    if normal.empty:
        raise ValueError('Guarded BARO needs a nonempty normal window')
    eligible = [
        name
        for name in frame.columns
        if name != 'time'
        and normal[name].quantile(0.75) - normal[name].quantile(0.25) > 0
    ]
    if not eligible:
        return []
    result = _BARO.baro(
        frame[['time', *eligible]],
        case.inject_time,
        dataset=case.task['dataset'],
    )
    return [str(name) for name in result['ranks']]
