"""Evaluate onset on tuning cases for free; held-out requires --final."""

import argparse
import json
from pathlib import Path

from incidents_agent.eval import onset
from incidents_agent.eval import selection
from incidents_agent.eval import split


def main() -> None:
    """Resolve cases, estimate onset without labels, then report error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', default='tuning')
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument(
        '--tasks-root', type=Path, default=Path('agent_tasks/re2-ob')
    )
    parser.add_argument(
        '--split', type=Path, default=Path('splits/re2-ob.json')
    )
    parser.add_argument(
        '--output', type=Path, default=Path('runs/phase-06/onset.json')
    )
    args = parser.parse_args()
    partition = split.from_json(json.loads(args.split.read_text()))
    try:
        ids = selection.resolve(
            args.cases, partition, final=args.final, limit=args.limit
        )
    except ValueError as error:
        parser.error(str(error))
    cases = {}
    for case_id in ids:
        folder = args.tasks_root / 'cases' / case_id
        task = json.loads((folder / 'task.json').read_text())
        cases[case_id] = (folder, float(task['inject_time']))
    result = onset.evaluate(cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in ('errors_s', 'failures')
            },
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
