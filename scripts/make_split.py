"""Create or verify the locked tuning and held-out split, and write subsets.

The first run builds `splits/<dataset>.json` from the answers file and a seed
fixed in this script. Later runs only verify the stored split; they never
rewrite it. Every run writes the tuning and held-out answers subsets beside
the full answers file, outside the blind task directory.

Only counts are printed. No per-case label is shown.
"""

import argparse
import json
import pathlib
import sys

from incidents_agent.eval import answers as answers_lib
from incidents_agent.eval import split as split_lib

SEED = 2026
N_TUNING_GROUPS = 10
_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', default='re2-ob')
    parser.add_argument('--tasks-root', type=pathlib.Path, default=None)
    parser.add_argument('--splits-dir', type=pathlib.Path, default=None)
    return parser.parse_args(argv)


def run(
    dataset: str, tasks_root: pathlib.Path, splits_dir: pathlib.Path
) -> split_lib.Split:
    """Create or verify the split and write the answers subsets.

    Args:
        dataset: The dataset name, for example `re2-ob`.
        tasks_root: The directory holding the task view and answers file.
        splits_dir: The directory holding committed split files.

    Returns:
        The split that was created or verified.
    """
    tasks_dir = tasks_root / dataset
    answers_path = tasks_root / f'{dataset}.answers.jsonl'
    split_path = splits_dir / f'{dataset}.json'
    answers = answers_lib.load_answers(answers_path)
    digest = answers_lib.file_sha256(answers_path)

    if split_path.exists():
        record = json.loads(split_path.read_text(encoding='utf-8'))
        split = split_lib.from_json(record)
        split_lib.verify(split, answers, digest)
        print(f'Verified the existing split {split_path.name}.')
    else:
        split = split_lib.make_split(
            answers,
            dataset=dataset,
            seed=SEED,
            n_tuning=N_TUNING_GROUPS,
            answers_sha256=digest,
        )
        splits_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(split.to_json(), indent=2) + '\n'
        split_path.write_text(text, encoding='utf-8')
        print(f'Created {split_path.name} (attempt {split.attempt}).')

    for side, case_ids in (
        ('tuning', split.tuning),
        ('heldout', split.heldout),
    ):
        out = tasks_root / f'{side}.answers.jsonl'
        chosen = answers_lib.subset(answers, case_ids)
        answers_lib.write_answers(chosen, out, tasks_dir)

    print(
        f'groups: {len(split.groups)}; '
        f'tuning: {len(split.tuning_groups)} groups, '
        f'{len(split.tuning)} cases; '
        f'held-out: {len(split.heldout_groups)} groups, '
        f'{len(split.heldout)} cases'
    )
    return split


def main(argv: list[str] | None = None) -> None:
    """Run from the command line."""
    args = _parse_args(argv)
    tasks_root = args.tasks_root or _ROOT / 'agent_tasks'
    splits_dir = args.splits_dir or _ROOT / 'splits'
    try:
        run(args.dataset, tasks_root, splits_dir)
    except (split_lib.SplitError, answers_lib.AnswersError) as error:
        sys.exit(f'make_split: {error}')


if __name__ == '__main__':
    main()
