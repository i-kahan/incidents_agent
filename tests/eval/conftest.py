"""Shared fixtures: a small hand-made answers file."""

import json
import pathlib

import pytest

from incidents_agent.eval import answers
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import split

# Six service-fault pairs, three repetitions each, interleaved by case id so
# grouping has to look at labels, not at id order.
PAIRS = [
    ('svc-a', 'cpu'),
    ('svc-b', 'mem'),
    ('svc-c', 'cpu'),
    ('svc-a', 'mem'),
    ('svc-b', 'cpu'),
    ('svc-c', 'mem'),
]


def _rows() -> list[dict[str, str]]:
    rows = []
    for number in range(18):
        service, fault = PAIRS[number % len(PAIRS)]
        rows.append(
            {
                'case_id': f'case-{number + 1:02d}',
                'dataset': 'toy',
                'service': service,
                'fault': fault,
                'indicator': fault,
            }
        )
    return rows


@pytest.fixture
def answers_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """A toy export: `toy/` task directory and `toy.answers.jsonl` beside it."""
    (tmp_path / 'toy').mkdir()
    path = tmp_path / 'toy.answers.jsonl'
    text = ''.join(json.dumps(row) + '\n' for row in _rows())
    path.write_text(text, encoding='utf-8')
    return path


@pytest.fixture
def evaluation_options(tmp_path):
    """Three tuning cases, a held-out ID and valid frozen free baselines."""
    groups = {'g1': ('a',), 'g2': ('b',), 'g3': ('c',), 'g4': ('held',)}
    partition = split.Split(
        'toy', 6, 1, 'hash', groups, ('g1', 'g2', 'g3'), ('g4',)
    )
    split_path = tmp_path / 'split.json'
    runner.atomic_json(split_path, partition.to_json())
    answer_rows = []
    predictions = []
    for case_id in ('a', 'b', 'c', 'held'):
        name = f'svc-{case_id}'
        folder = tmp_path / 'tasks/cases' / case_id
        folder.mkdir(parents=True)
        (folder / 'metrics.csv').write_text(
            f'time,{name}_cpu\n'
            + ''.join(
                f'{1700000000 + i * 10},{10 + i % 3 + 100 * (i > 30)}\n'
                for i in range(60)
            )
        )
        runner.atomic_json(
            folder / 'task.json',
            {
                'case_id': case_id,
                'candidate_services': [name],
                'candidate_indicators': [f'{name}_cpu'],
                'inject_time': 1700000300,
            },
        )
        answer_rows.append(
            {
                'case_id': case_id,
                'dataset': 're2-ob',
                'service': name,
                'fault': 'cpu',
                'indicator': 'cpu',
                'answer_in_candidates': True,
                'indicator_in_candidates': True,
                'n_candidate_services': 1,
                'n_candidate_indicators': 1,
            }
        )
        predictions.append({'case_id': case_id, 'ranks': [f'{name}_cpu']})
    answer_path = tmp_path / 'answers.jsonl'
    answer_path.write_text(
        ''.join(json.dumps(row) + '\n' for row in answer_rows)
    )
    baseline_dir = tmp_path / 'runs/phase-02'
    baseline_dir.mkdir(parents=True)
    for name in ('baro', 'guarded_baro'):
        (baseline_dir / f'{name}.tuning.predictions.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in predictions[:3])
        )
        (baseline_dir / f'{name}.heldout.predictions.jsonl').write_text(
            json.dumps(predictions[3]) + '\n'
        )
    runner.atomic_json(
        tmp_path / 'results/phase-02/reference_baseline.json',
        {
            'baseline': 'guarded_baro',
            'tuning_ids': list(partition.tuning),
            'split_sha256': answers.file_sha256(split_path),
            'predictions_sha256': answers.file_sha256(
                baseline_dir / 'guarded_baro.tuning.predictions.jsonl'
            ),
        },
    )
    return service.Options(
        dry_run=True,
        agent='single_call',
        split=split_path,
        tasks_root=tmp_path / 'tasks',
        answers=answer_path,
        baseline_root=tmp_path,
        output_root=tmp_path / 'eval-runs',
    )
