"""Prepare a frozen tuning audit or summarize human-entered judgments."""

import argparse
import json
from pathlib import Path

from incidents_agent.eval import audit
from incidents_agent.eval import split


def main() -> None:
    """Read a report map for preparation, or a template and filled copy."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument(
        'reports', type=Path, help='JSON case_id -> report map'
    )
    prepare.add_argument(
        '--split', type=Path, default=Path('splits/re2-ob.json')
    )
    prepare.add_argument('--seed', type=int, default=6)
    summarize = commands.add_parser('summarize')
    summarize.add_argument('template', type=Path)
    summarize.add_argument('completed', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        partition = split.from_json(json.loads(args.split.read_text()))
        result = audit.prepare(
            json.loads(args.reports.read_text()),
            partition.tuning,
            seed=args.seed,
        )
    else:
        result = audit.summarize(
            json.loads(args.template.read_text()),
            json.loads(args.completed.read_text()),
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
