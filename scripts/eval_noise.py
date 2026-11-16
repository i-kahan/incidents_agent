"""Measure top-1 noise from completed repeats without calling a model."""

import argparse
import json
from pathlib import Path

from incidents_agent.eval import reporting


def main() -> None:
    """Require distinct repeat IDs and identical inputs/configurations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = reporting.repeat_report(args.runs, args.output)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
