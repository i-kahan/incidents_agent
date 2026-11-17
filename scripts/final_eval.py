"""Freeze, run, and publish the Phase 10 evaluation."""

import argparse
from pathlib import Path

from incidents_agent.eval import publication
from incidents_agent.eval import release
from incidents_agent.eval import runner
from incidents_agent.eval import service


def main() -> int:
    """Dispatch the release command using the shared repository state."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--evidence', type=Path, required=True)
    run = commands.add_parser('run')
    run.add_argument('arm', choices=release.ARMS)
    publish = commands.add_parser('publish')
    publish.add_argument('--out', type=Path, default=Path('results/final'))
    omit = commands.add_parser('omit')
    omit.add_argument('arm', choices=release.OPTIONAL)
    omit.add_argument('--reason', required=True)
    args = parser.parse_args()
    shared = service.shared_root()
    root = shared / 'runs/phase-10'
    try:
        if args.command == 'freeze':
            release.freeze(root, shared, args.evidence)
        elif args.command == 'run':
            print(release.run_arm(root, shared, args.arm))
        elif args.command == 'omit':
            with release.claim(root):
                runner.read_object(root / 'release.json')
                if (root / f'{args.arm}.complete.json').exists():
                    raise ValueError('Cannot omit a completed arm')
                runner.atomic_json(
                    root / f'{args.arm}.omitted.json', {'reason': args.reason}
                )
        else:
            publication.publish(root, shared, args.out)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
