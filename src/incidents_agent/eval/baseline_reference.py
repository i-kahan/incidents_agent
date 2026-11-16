"""Validate the tuning-selected baseline before comparing new predictions."""

import json
from pathlib import Path

from incidents_agent.eval import answers
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import split


def compare(
    folder: Path,
    agent: scoring.ScoredRun,
    *,
    reference: Path,
    baseline_runs: Path,
    split_path: Path,
    answers_path: Path,
    tasks_dir: Path,
) -> dict[str, object]:
    """Use the full-tuning winner without selecting on held-out data."""
    if not reference.exists():
        result = {
            'available': False,
            'reason': (
                'Run make baselines to freeze the tuning-selected reference.'
            ),
        }
        runner.atomic_json(folder / 'baseline-comparison.json', result)
        return result
    chosen = runner.read_object(reference)
    partition = split.from_json(json.loads(split_path.read_text()))
    if chosen.get('split_sha256') != answers.file_sha256(
        split_path
    ) or chosen.get('tuning_ids') != list(partition.tuning):
        raise ValueError('Baseline selection belongs to another split')
    name = chosen.get('baseline')
    if name not in ('baro', 'guarded_baro'):
        raise ValueError('Unknown baseline selection')
    tuning = baseline_runs / f'{name}.tuning.predictions.jsonl'
    if answers.file_sha256(tuning) != chosen.get('predictions_sha256'):
        raise ValueError('Baseline tuning predictions changed after selection')
    intended = {case.case_id for case in agent.cases}
    records = {}
    sides = []
    if intended & set(partition.tuning):
        sides.append('tuning')
    if intended & set(partition.heldout):
        sides.append('heldout')
    for side in sides:
        for line in (
            (baseline_runs / f'{name}.{side}.predictions.jsonl')
            .read_text()
            .splitlines()
        ):
            if not line.strip():
                continue
            record = json.loads(line)
            case_id = record['case_id']
            if case_id in records:
                raise ValueError('Duplicate baseline prediction')
            records[case_id] = record
    selected = [
        records[case_id] for case_id in sorted(intended) if case_id in records
    ]
    path = folder / 'baseline.predictions.jsonl'
    path.write_text(''.join(json.dumps(record) + '\n' for record in selected))
    baseline = scoring.score_subset(
        answers_path,
        path,
        sorted(intended),
        subset_path=folder / 'baseline.answers.jsonl',
        tasks_dir=tasks_dir,
    )
    return reporting.compare_baseline(folder, agent, baseline, name=str(name))
