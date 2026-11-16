"""Exact-subset official scoring and lossless evaluation-facing records."""

import dataclasses
import json
import pathlib
from collections.abc import Mapping
from collections.abc import Sequence

from incidents_agent.eval import answers
from incidents_agent.eval import rcaeval_bridge


@dataclasses.dataclass(frozen=True)
class CaseScore:
    """Official ranks and grouping labels for one intended benchmark case."""

    case_id: str
    service: str
    fault: str
    indicator: str | None
    service_rank: int | None
    indicator_rank: int | None
    top_service: str | None
    inspected_root: bool | None = None

    @property
    def group(self) -> tuple[str, str]:
        """Return the service-fault cluster."""
        return self.service, self.fault

    def hit(self, level: str, k: int) -> float | None:
        """Return official top-k correctness, or indicator ineligibility."""
        if level not in ('service', 'indicator') or k not in (1, 3, 5):
            raise ValueError('Expected service/indicator and k=1,3,5')
        if level == 'indicator' and self.indicator is None:
            return None
        rank = self.service_rank if level == 'service' else self.indicator_rank
        return float(rank is not None and rank <= k)


@dataclasses.dataclass(frozen=True)
class ScoredRun:
    """Official point estimates together with typed per-case results."""

    summary: Mapping[str, object]
    cases: tuple[CaseScore, ...]


def _rank(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError('Invalid evaluator rank')
    return value


def parse_case(row: Mapping[str, object]) -> CaseScore:
    """Validate and convert an official evaluator row."""
    labels = [row.get(key) for key in ('case_id', 'service', 'fault')]
    if not all(isinstance(value, str) and value for value in labels):
        raise ValueError('Evaluator row lacks case_id/service/fault')
    top = row.get('top5')
    if not isinstance(top, list) or not all(isinstance(v, str) for v in top):
        raise ValueError('Evaluator row lacks top5')
    inspected = row.get('inspected_root')
    if inspected is not None and type(inspected) is not bool:
        raise ValueError('Invalid inspected_root')
    indicator = row.get('indicator')
    if indicator is not None and not isinstance(indicator, str):
        raise ValueError('Invalid indicator')
    return CaseScore(
        str(labels[0]),
        str(labels[1]),
        str(labels[2]),
        indicator,
        _rank(row.get('service_rank')),
        _rank(row.get('indicator_rank')),
        rcaeval_bridge.service_name(top[0]) if top else None,
        inspected,
    )


def score_subset(
    answers_path: pathlib.Path,
    predictions_path: pathlib.Path,
    case_ids: Sequence[str],
    *,
    subset_path: pathlib.Path,
    tasks_dir: pathlib.Path,
    aggregate_only: bool = False,
) -> ScoredRun:
    """Score exactly the intended cases, keeping absent predictions as misses.

    The subset is written outside the blind task view. Unexpected prediction
    IDs and duplicates are rejected by the unchanged official evaluator.
    """
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValueError('Intended cases must be nonempty and unique')
    selected = answers.subset(answers.load_answers(answers_path), case_ids)
    subset_path.parent.mkdir(parents=True, exist_ok=True)
    answers.write_answers(selected, subset_path, tasks_dir)
    summary, rows = rcaeval_bridge.score(
        subset_path,
        predictions_path,
        expected_cases=len(case_ids),
        aggregate_only=aggregate_only,
    )
    cases = tuple(parse_case(row) for row in rows)
    if not aggregate_only and {c.case_id for c in cases} != set(case_ids):
        raise rcaeval_bridge.ScoringError('Evaluator returned different cases')
    return ScoredRun(summary, cases)


def export_trajectory(
    events: Sequence[Mapping[str, object]],
    path: pathlib.Path,
) -> None:
    """Write explicit target projections for RCAEval 1.12's narrow reader.

    These derived records are not additional tool executions. The canonical
    agent trajectory is retained separately; counts come from that record.
    Only successful factual targets before the final report are projected.
    """
    records: list[dict[str, object]] = []
    for event in events:
        if event.get('type') == 'final_report':
            break
        if event.get('type') != 'tool_call':
            continue
        stored = event.get('stored', event)
        if not isinstance(stored, dict) or stored.get('ok') is not True:
            continue
        targets = stored.get('target_components', [])
        if not isinstance(targets, (list, tuple)):
            raise ValueError('Invalid target_components')
        for target in sorted(set(str(t) for t in targets if t)):
            records.append(
                {
                    'type': 'tool_call',
                    'tool': 'observed_target',
                    'args': {'service': rcaeval_bridge.service_name(target)},
                    'derived': True,
                    'source_call_id': stored.get('call_id'),
                    'source_tool': stored.get('tool'),
                }
            )
    records.append({'type': 'tool_call', 'tool': 'submit', 'args': {}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ''.join(json.dumps(record) + '\n' for record in records),
        encoding='utf-8',
    )
