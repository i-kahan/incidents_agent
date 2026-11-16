"""Shared fixtures: a small hand-made answers file."""

import json
import pathlib

import pytest

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
