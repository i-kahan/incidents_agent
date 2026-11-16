"""Sequential cached model comparisons against a frozen free reference."""

import dataclasses
import json
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from incidents_agent.eval import baseline_reference
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import service
from incidents_agent.eval import split
from incidents_agent.eval import stats
from incidents_agent.eval import table_output


def baseline_records(
    name: str,
    prepared: service.Prepared,
) -> list[dict[str, object]]:
    """Read baseline sides, rejecting duplicates and malformed rows."""
    partition = split.from_json(json.loads(prepared.options.split.read_text()))
    ids = {case.case_id for case in prepared.cases}
    records: dict[str, dict[str, object]] = {}
    for side, side_ids in (
        ('tuning', partition.tuning),
        ('heldout', partition.heldout),
    ):
        if not ids.intersection(side_ids):
            continue
        path = (
            prepared.baseline_root
            / 'runs/phase-02'
            / f'{name}.{side}.predictions.jsonl'
        )
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('case_id') not in side_ids:
                raise ValueError('Foreign or malformed baseline prediction')
            case_id = str(row['case_id'])
            ranks = row.get('ranks')
            if (
                case_id in records
                or not isinstance(ranks, list)
                or not all(isinstance(rank, str) for rank in ranks)
            ):
                raise ValueError('Duplicate or invalid baseline prediction')
            records[case_id] = row
    return [
        records.get(case_id, {'case_id': case_id, 'ranks': []})
        for case_id in sorted(ids)
    ]


def estimate(value: stats.Estimate) -> str:
    """Format a point and interval without inventing unavailable bounds."""
    if value.value is None:
        return 'N/A'
    if value.low is None or value.high is None:
        return f'{value.value:.3f} [N/A: {value.reason}]'
    return f'{value.value:.3f} [{value.low:.3f}, {value.high:.3f}]'


def table_row(
    label: str,
    scores: scoring.ScoredRun,
    reference: scoring.ScoredRun,
    records: Sequence[dict[str, object]],
    *,
    free: bool = False,
) -> dict[str, object]:
    """Build accuracy and measured-resource columns for one arm."""
    row: dict[str, object] = {'model': label, 'cases': len(scores.cases)}
    for level in ('service', 'indicator'):
        for k in (1, 3, 5):
            row[f'{level}_AC@{k} (95% CI)'] = estimate(
                stats.accuracy(scores.cases, level, k)
            )
    for metric in (
        'cost_usd',
        'input_tokens',
        'output_tokens',
        'tool_calls',
        'wall_time_s',
    ):
        measurements = [
            float(usage[metric])
            for record in records
            if isinstance(usage := record.get('usage'), dict)
            and type(usage.get(metric)) in (int, float)
        ]
        if free and metric != 'wall_time_s':
            measurements = [0.0] * len(scores.cases)
        row[f'{metric}/case'] = (
            sum(measurements) / len(measurements) if measurements else None
        )
        row[f'{metric}_n'] = len(measurements)
    row['service_AC@1 delta (95% CI)'] = estimate(
        stats.paired_difference(
            scores.cases,
            reference.cases,
            'service',
            1,
        )
    )
    return row


def matching_noise(
    folders: Sequence[Path],
    prepared: Sequence[service.Prepared],
    output: Path,
) -> dict[str, object]:
    """Validate matching repeats and identify the applicable comparison arm."""
    if not folders:
        return {'available': False, 'reason': 'Not measured'}
    frozen = runner.read_object(folders[0] / 'manifest.json')['frozen']
    if not isinstance(frozen, dict) or not isinstance(
        frozen.get('cases'), list
    ):
        raise ValueError('Invalid noise manifest')
    noise_cases = {
        entry['case_id']: entry['files'] for entry in frozen['cases']
    }
    matched = []
    for arm in prepared:
        cases = arm.manifest['cases']
        if not isinstance(cases, list):
            raise ValueError('Invalid comparison manifest')
        actual = {entry['case_id']: entry['files'] for entry in cases}
        if (
            frozen['configuration_fingerprint']
            == arm.manifest['configuration_fingerprint']
            and frozen['dry_run'] == arm.options.dry_run
            and all(
                actual.get(key) == value for key, value in noise_cases.items()
            )
        ):
            matched.append(arm.options.model)
    if not matched:
        raise ValueError(
            'Noise runs do not match any comparison configuration/input subset'
        )
    result = reporting.repeat_report(folders, output)
    return {'available': True, 'models': matched, 'measurements': result}


def run(
    options: service.Options,
    models: Sequence[str],
    *,
    results_root: Path | None = None,
    noise_runs: Sequence[Path] = (),
) -> tuple[Path, str]:
    """Preflight all arms, then execute sequentially through the cache."""
    if not models or len(set(models)) != len(models):
        raise ValueError('Models must be nonempty and unique')
    if options.cases == 'field':
        raise ValueError('Model comparison supports RCAEval only')
    if options.resume is not None or options.model is not None:
        raise ValueError('Use --models; rerun a comparison to reuse its cache')
    arms = [
        service.prepare(dataclasses.replace(options, model=model))
        for model in models
    ]
    first = arms[0]
    if any(arm.manifest['cases'] != first.manifest['cases'] for arm in arms):
        raise ValueError('Comparison case inputs changed during preflight')
    try:
        reference_name = baseline_reference.validate(
            first.baseline_root / 'results/phase-02/reference_baseline.json',
            first.baseline_root / 'runs/phase-02',
            options.split,
        )
        saved_baselines = {
            name: baseline_records(name, first)
            for name in ('baro', 'guarded_baro')
        }
    except (ValueError, OSError) as error:
        raise ValueError(
            f'Invalid baseline artifacts; run make baselines. {error}'
        ) from error
    root = results_root or service.shared_root() / 'results/phase-06'
    folder = root / ('dry' if options.dry_run else 'real') / uuid.uuid4().hex
    folder.mkdir(parents=True)
    noise = matching_noise(noise_runs, arms, folder / 'noise.json')
    runner.atomic_json(
        folder / 'manifest.json',
        {
            'models': list(models),
            'agent': options.agent,
            'dry_run': options.dry_run,
            'reference': reference_name,
            'runs': [arm.manifest for arm in arms],
        },
    )
    baselines = {}
    with tempfile.TemporaryDirectory() as temporary:
        for name, records in saved_baselines.items():
            target = Path(temporary) / name
            target.mkdir()
            predictions = reporting.write_predictions(target, records)
            baselines[name] = scoring.score_subset(
                first.answers_path,
                predictions,
                [case.case_id for case in first.cases],
                subset_path=target / 'answers.jsonl',
                tasks_dir=first.tasks_root,
            )
    reference = baselines[reference_name]
    rows = [
        table_row(name, scores, reference, saved_baselines[name], free=True)
        for name, scores in baselines.items()
    ]
    noise_models = noise.get('models', [])
    if not isinstance(noise_models, list):
        raise ValueError('Invalid noise model list')
    details: dict[str, object] = {
        'reference': reference_name,
        'noise': noise,
        'models': {},
    }
    model_details: dict[str, object] = {}
    for model, arm in zip(models, arms, strict=True):
        result = service.execute(arm, compare_reference=False)
        if result.scored is None:
            raise ValueError('Expected benchmark scores')
        paired = reporting.compare_baseline(
            result.folder, result.scored, reference, name=reference_name
        )
        paired['noise'] = noise if model in noise_models else 'Not measured'
        runner.atomic_json(result.folder / 'baseline-comparison.json', paired)
        row = table_row(model, result.scored, reference, result.records)
        rows.append(row)
        model_details[model] = {
            'run': str(result.folder),
            'manifest': result.manifest,
            'scores': runner.read_object(result.folder / 'summary.json'),
            'usage': result.usage,
            'paired': paired,
            'invocation_cost_usd': result.invocation_cost_usd,
        }
        details['models'] = model_details
        runner.atomic_json(folder / 'comparison.json', details)
    for row in rows:
        row['noise'] = (
            json.dumps(noise['measurements'], sort_keys=True)
            if row['model'] in noise_models
            else 'Not measured'
        )
    markdown = table_output.write(
        folder / 'comparison',
        rows,
        note=(
            f'Reference: {reference_name}, selected on full tuning. '
            '95% service–fault clustered intervals; seed 6, 10,000 draws. '
            'Resource means cover measured cases; *_n gives coverage. '
            'Costs include historical cached execution and all attempts; '
            'invocation spend is recorded separately. Touching a service '
            'does not establish diagnosis quality.'
            + (
                ' Dry-run model results are synthetic.'
                if options.dry_run
                else ''
            )
        ),
    )
    return folder, markdown
