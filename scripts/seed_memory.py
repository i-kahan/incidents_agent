"""Seed a frozen memory bundle from tuning cases without model calls."""

import argparse
import json
from pathlib import Path

from incidents_agent.eval import memory_seed
from incidents_agent.eval import service


def main(argv: list[str] | None = None) -> int:
    """Validate tuning inputs, publish a bundle, and print its identity."""
    shared = service.shared_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--split', type=Path, default=service.ROOT / 'splits/re2-ob.json'
    )
    parser.add_argument(
        '--tasks-root', type=Path, default=shared / 'agent_tasks/re2-ob'
    )
    parser.add_argument(
        '--answers',
        type=Path,
        default=shared / 'agent_tasks/tuning.answers.jsonl',
    )
    parser.add_argument(
        '--out', type=Path, default=shared / 'runs/phase-09/memory-seed'
    )
    args = parser.parse_args(argv)
    try:
        manifest = memory_seed.seed(
            args.split, args.tasks_root, args.answers, args.out
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    case_ids = manifest['case_ids']
    assert isinstance(case_ids, list)
    print(
        json.dumps(
            {
                'output': str(args.out.resolve()),
                'records': len(case_ids),
                'memory_snapshot_hash': manifest['memory_snapshot_hash'],
            },
            indent=2,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
