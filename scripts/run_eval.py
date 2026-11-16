"""Run one cached agent or single-call baseline configuration."""

import argparse
import json

from incidents_agent.eval import service


def main() -> int:
    """Prepare, execute, and print the saved evaluation summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    service.add_arguments(parser)
    args = parser.parse_args()
    try:
        result = service.execute(
            service.prepare(service.Options.from_args(args))
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                'output': str(result.folder),
                'dry_run': args.dry_run,
                'summary': result.summary,
            },
            indent=2,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
