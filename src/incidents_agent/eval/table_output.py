"""Consistent CSV and Markdown tables for saved evaluation reports."""

import csv
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path


def write(
    stem: Path, rows: Sequence[Mapping[str, object]], *, note: str = ''
) -> str:
    """Write a flat table in two formats and return its Markdown text."""
    if not rows:
        raise ValueError('Cannot report an empty table')
    stem.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0])
    with stem.with_suffix('.csv').open(
        'w', newline='', encoding='utf-8'
    ) as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    def cell(value: object) -> str:
        text = 'N/A' if value is None else str(value)
        return text.replace('|', '\\|').replace('\n', ' ')

    lines = [
        '| ' + ' | '.join(columns) + ' |',
        '| ' + ' | '.join('---' for _ in columns) + ' |',
    ]
    lines.extend(
        '| ' + ' | '.join(cell(row.get(key)) for key in columns) + ' |'
        for row in rows
    )
    markdown = '\n'.join(lines) + '\n\n' + note + '\n'
    stem.with_suffix('.md').write_text(markdown, encoding='utf-8')
    return markdown
