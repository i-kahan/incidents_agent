"""Compare explicitly listed models using the shared evaluation cache."""

import argparse
from pathlib import Path

from incidents_agent.eval import comparison
from incidents_agent.eval import service


def main() -> int:
    """Run one mode across models and write Markdown, CSV and JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    service.add_arguments(parser)
    parser.add_argument('--models', nargs='+', required=True)
    parser.add_argument('--results-root', type=Path)
    parser.add_argument('--noise-runs', nargs='+', type=Path, default=[])
    args = parser.parse_args()
    try:
        folder, table = comparison.run(
            service.Options.from_args(args),
            args.models,
            results_root=args.results_root,
            noise_runs=args.noise_runs,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(table)
    print(f'Output: {folder}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
