"""Report saved benchmark failures without running any models."""

import argparse
from pathlib import Path

from incidents_agent.eval import failures
from incidents_agent.eval import service


def main() -> int:
    """Write one row per intended case and category counts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--final', action='store_true')
    parser.add_argument(
        '--split', type=Path, default=service.ROOT / 'splits/re2-ob.json'
    )
    args = parser.parse_args()
    try:
        folder, table = failures.run(
            args.run,
            output=args.output,
            final=args.final,
            split_path=args.split,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(table)
    print(f'Output: {folder}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
