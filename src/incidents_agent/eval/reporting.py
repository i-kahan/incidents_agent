"""Separate benchmark/field reports and trajectory aggregate summaries."""

import dataclasses
import json
import os
from collections import Counter
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from incidents_agent.eval import agent_adapter
from incidents_agent.eval import field
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import stats


def summarize_usage(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Aggregate measured usage and outcomes without inventing missing data."""
    outcomes: Counter[str] = Counter()
    stops: Counter[str] = Counter()
    totals: dict[str, float] = {}
    counts: Counter[str] = Counter()
    for record in records:
        usage = record.get('usage')
        if not isinstance(usage, dict):
            usage = {}
        outcomes[str(usage.get('outcome', 'execution_error'))] += 1
        stops[
            str(usage.get('stop_reason', record.get('stop_reason', 'unknown')))
        ] += 1
        for key, value in usage.items():
            if type(value) in (int, float):
                totals[str(key)] = totals.get(str(key), 0) + float(value)
                counts[str(key)] += 1
    attempted = totals.get('citation_attempts', 0)
    return {
        'n_cases': len(records),
        'outcome_counts': dict(outcomes),
        'outcome_shares': {k: n / len(records) for k, n in outcomes.items()},
        'stop_reasons': dict(stops),
        'usage': {
            key: {
                'total': value,
                'n_measured': counts[key],
                'mean': value / counts[key],
            }
            for key, value in totals.items()
            if key not in ('citation_validity',)
        },
        'citation_validity': totals.get('valid_citation_attempts', 0)
        / attempted
        if attempted
        else None,
    }


def write_predictions(
    folder: Path, records: Sequence[Mapping[str, object]]
) -> Path:
    """Export official predictions with relocatable trajectory references."""
    path = folder / 'predictions.jsonl'
    lines = []
    for record in records:
        prediction = {key: record[key] for key in ('case_id', 'ranks')}
        prediction['model'] = record.get('model')
        prediction['usage'] = record.get('usage', {})
        target = record.get('trajectory')
        if isinstance(target, str):
            prediction['trajectory'] = os.path.relpath(target, folder)
        lines.append(json.dumps(prediction) + '\n')
    temp = path.with_suffix('.tmp')
    temp.write_text(''.join(lines), encoding='utf-8')
    temp.replace(path)
    return path


def field_report(
    folder: Path,
    records: Sequence[Mapping[str, object]],
    gold_dir: Path,
) -> dict[str, object]:
    """Score field counts, writing their own JSON and Markdown table."""
    rows = []
    for record in records:
        case_id = str(record['case_id'])
        gold = field.load_gold(gold_dir / f'{case_id}.json')
        raw = record.get('field_prediction')
        prediction = (
            agent_adapter.restore_prediction(raw)
            if isinstance(raw, dict)
            else field.Prediction((), None, 'inconclusive')
        )
        rows.append(field.score(gold, prediction))
    summary = field.summarize(rows)
    runner.atomic_json(
        folder / 'field-summary.json', {**summary, 'cases': rows}
    )
    counts = summary['counts']
    if not isinstance(counts, dict):
        raise ValueError('Invalid field counts')
    table = ['# Field fixtures', '', '| Measure | Count |', '|---|---:|']
    table.extend(f'| {key} | {count}/7 |' for key, count in counts.items())
    table.extend(['', str(summary['caveat']), ''])
    (folder / 'field-summary.md').write_text('\n'.join(table), encoding='utf-8')
    return summary


def benchmark_report(
    folder: Path,
    records: Sequence[Mapping[str, object]],
    *,
    answers_path: Path,
    tasks_dir: Path,
    draws: int = 10_000,
) -> scoring.ScoredRun:
    """Write official point estimates and clustered 95% intervals."""
    predictions = write_predictions(folder, records)
    result = scoring.score_subset(
        answers_path,
        predictions,
        [str(record['case_id']) for record in records],
        subset_path=folder / 'answers-subset.jsonl',
        tasks_dir=tasks_dir,
    )
    intervals = {
        f'{level}_AC@{k}': dataclasses.asdict(
            stats.accuracy(result.cases, level, k, draws=draws)
        )
        for level in ('service', 'indicator')
        for k in (1, 3, 5)
    }
    runner.atomic_json(
        folder / 'summary.json',
        {
            'official': dict(result.summary),
            'intervals': intervals,
            'seed': 6,
            'draws': draws,
            'confidence': 0.95,
            'inspection_caveat': (
                'Touching a service does not establish diagnosis quality.'
            ),
        },
    )
    runner.atomic_json(
        folder / 'case-scores.json',
        {'cases': [dataclasses.asdict(case) for case in result.cases]},
    )
    return result


def compare_baseline(
    folder: Path,
    agent: scoring.ScoredRun,
    baseline: scoring.ScoredRun,
    *,
    name: str,
    draws: int = 10_000,
) -> dict[str, object]:
    """Persist paired differences and agreement against the frozen reference."""
    comparison: dict[str, object] = {
        'baseline': name,
        'differences': {
            f'{level}_AC@{k}': dataclasses.asdict(
                stats.paired_difference(
                    agent.cases, baseline.cases, level, k, draws=draws
                )
            )
            for level in ('service', 'indicator')
            for k in (1, 3, 5)
        },
        'mcnemar_top1': {
            level: stats.mcnemar(agent.cases, baseline.cases, level)
            for level in ('service', 'indicator')
        },
        'agreement': stats.agreement(agent.cases, baseline.cases),
        'noise': ('Not measured; run matched repeats to estimate noise.'),
    }
    runner.atomic_json(folder / 'baseline-comparison.json', comparison)
    return comparison


def repeat_report(folders: Sequence[Path], output: Path) -> dict[str, object]:
    """Measure noise only across distinct repeats of identical frozen inputs."""
    repeats = []
    fingerprints = []
    identities = []
    for folder in folders:
        manifest = runner.read_object(folder / 'manifest.json').get('frozen')
        if not isinstance(manifest, dict):
            raise ValueError('Run has no frozen manifest')
        cases = manifest.get('cases')
        if not isinstance(cases, list):
            raise ValueError('Manifest cases must be a list')
        fingerprints.append(
            runner.digest(
                {
                    'configuration': manifest['configuration_fingerprint'],
                    'dry_run': manifest['dry_run'],
                    'cases': [
                        {k: case[k] for k in ('case_id', 'files', 'suite')}
                        for case in cases
                    ],
                }
            )
        )
        identities.append(manifest['repeat_id'])
        saved = runner.read_object(folder / 'case-scores.json').get('cases')
        if not isinstance(saved, list):
            raise ValueError('Run has no benchmark case scores')
        repeats.append(
            tuple(
                scoring.parse_case(
                    {
                        **case,
                        'top5': [case['top_service']]
                        if case['top_service']
                        else [],
                    }
                )
                for case in saved
            )
        )
    if len(set(identities)) != len(identities):
        raise ValueError(
            'Noise needs distinct repeat IDs, not cached duplicates'
        )
    result = stats.noise(repeats, fingerprints)
    runner.atomic_json(output, result)
    return result
