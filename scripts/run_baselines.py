"""Run the free baselines on every case and score them by split side.

Writes predictions for BARO as RCAEval runs it by default and for
multi-source BARO, plus each kept to candidate services where that differs.
Tuning is scored in full: the summary is committed under `results/phase-02/`
and the per-case rows stay in gitignored `runs/`.
Held-out is scored for aggregate numbers only; nothing per case or per
fault is written or printed.
"""

import json
import pathlib
import sys
from collections.abc import Callable
from collections.abc import Mapping

from incidents_agent.eval import rcaeval_bridge
from incidents_agent.eval import split as split_lib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
DATASET = 're2-ob'
TASKS_ROOT = _ROOT / 'agent_tasks'
RUNS_DIR = _ROOT / 'runs' / 'phase-02'
RESULTS_DIR = _ROOT / 'results' / 'phase-02'

Ranker = Callable[[pathlib.Path], list[str]]


def _rank_all(
    case_ids: list[str], ranker: Ranker, label: str
) -> dict[str, list[str]]:
    """Rank every case; a case that fails gets an empty ranking (a miss)."""
    ranks = {}
    failures = 0
    for case_id in case_ids:
        case_dir = TASKS_ROOT / DATASET / 'cases' / case_id
        try:
            ranks[case_id] = ranker(case_dir)
        except (KeyError, ValueError, FileNotFoundError) as error:
            failures += 1
            print(
                f'{label}: {type(error).__name__} on one case', file=sys.stderr
            )
            ranks[case_id] = []
    print(f'{label}: ranked {len(case_ids) - failures}/{len(case_ids)} cases')
    return ranks


def _candidates_only(ranks: Mapping[str, list[str]]) -> dict[str, list[str]]:
    kept = {}
    dropped = 0
    for case_id, names in ranks.items():
        services = rcaeval_bridge.candidate_services(
            TASKS_ROOT / DATASET / 'cases' / case_id
        )
        kept[case_id] = rcaeval_bridge.keep_candidates(names, services)
        dropped += len(names) - len(kept[case_id])
    print(f'dropped {dropped} ranked names with a non-candidate service')
    return kept


def _write_json(path: pathlib.Path, record: Mapping[str, object]) -> None:
    path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')


def _headline(summary: Mapping[str, object]) -> str:
    parts = []
    for level in ('service', 'indicator'):
        scores = summary[level]
        if isinstance(scores, dict):
            parts.append(
                f'{level} AC@1 {scores["AC@1"]:.3f} AC@3 {scores["AC@3"]:.3f} '
                f'AC@5 {scores["AC@5"]:.3f}'
            )
    return '; '.join(parts)


def score_sides(
    model: str, ranks: Mapping[str, list[str]], split: split_lib.Split
) -> None:
    """Write one model's predictions per side and score each side.

    Args:
        model: The prediction set's name.
        ranks: Ranked names for every case.
        split: The locked split.
    """
    paths = {}
    for side, case_ids in (
        ('tuning', split.tuning),
        ('heldout', split.heldout),
    ):
        paths[side] = RUNS_DIR / f'{model}.{side}.predictions.jsonl'
        rcaeval_bridge.write_predictions(
            paths[side], {c: ranks[c] for c in case_ids}, model
        )
    tuning, rows = rcaeval_bridge.score(
        TASKS_ROOT / 'tuning.answers.jsonl',
        paths['tuning'],
        expected_cases=len(split.tuning),
        aggregate_only=False,
    )
    rows_text = ''.join(json.dumps(r) + '\n' for r in rows)
    (RUNS_DIR / f'{model}_tuning.rows.jsonl').write_text(
        rows_text, encoding='utf-8'
    )
    _write_json(RESULTS_DIR / f'{model}_tuning.json', tuning)

    heldout, _ = rcaeval_bridge.score(
        TASKS_ROOT / 'heldout.answers.jsonl',
        paths['heldout'],
        expected_cases=len(split.heldout),
        aggregate_only=True,
    )
    _write_json(RESULTS_DIR / f'{model}_heldout.json', heldout)
    print(f'{model} tuning:   {_headline(tuning)}')
    print(f'{model} held-out: {_headline(heldout)}')


def main() -> None:
    """Rank all cases with each baseline, then score each split side."""
    record = json.loads(
        (_ROOT / 'splits' / f'{DATASET}.json').read_text(encoding='utf-8')
    )
    split = split_lib.from_json(record)
    case_ids = sorted(split.tuning + split.heldout)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    baro = _rank_all(case_ids, rcaeval_bridge.baro_ranks, 'baro')
    mmbaro = _rank_all(case_ids, rcaeval_bridge.mmbaro_ranks, 'mmbaro')
    runs = {'baro': baro, 'mmbaro': mmbaro}
    for model, ranks in list(runs.items()):
        kept = _candidates_only(ranks)
        if kept != ranks:
            runs[f'{model}_candidates'] = kept
    for model, ranks in runs.items():
        path = RUNS_DIR / f'{model}.predictions.jsonl'
        rcaeval_bridge.write_predictions(path, ranks, model)
        score_sides(model, ranks, split)


if __name__ == '__main__':
    main()
