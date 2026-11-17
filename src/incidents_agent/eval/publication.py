"""Build portable Phase 10 evidence from saved runs without model clients."""

import json
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from incidents_agent.agent import trajectory
from incidents_agent.eval import comparison
from incidents_agent.eval import failures
from incidents_agent.eval import onset
from incidents_agent.eval import release
from incidents_agent.eval import reporting
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import service
from incidents_agent.eval import table_output
from incidents_agent.llm import ledger

INPUTS = {
    'primary': 'Blind exported telemetry, candidates and supplied fault start',
    'single-call': 'One model call with a bounded telemetry summary',
    'baro': 'Metrics and supplied fault start',
    'guarded_baro': 'BARO with candidate filtering',
    'mmbaro': 'Metrics, log templates, trace counts and durations',
    'mmbaro_candidates': 'Multi-source BARO with candidate filtering',
    'memory': 'Primary inputs plus frozen tuning incident memory',
    'multi': 'Primary inputs with hypothesis testers and a skeptic',
    'rcaeval-reference': 'Blind RCAEval inputs via the upstream toolkit',
}


def aligned(
    rows: Iterable[dict[str, object]], ids: set[str]
) -> list[dict[str, object]]:
    """Require one valid prediction per intended case, including empty ranks."""
    result = list(rows)
    seen = set()
    for row in result:
        case_id = row.get('case_id')
        ranks = row.get('ranks')
        if (
            not isinstance(case_id, str)
            or case_id in seen
            or not isinstance(ranks, list)
            or not all(isinstance(rank, str) for rank in ranks)
        ):
            raise ValueError('Duplicate or invalid prediction')
        seen.add(case_id)
    if seen != ids:
        raise ValueError('Predictions do not cover the frozen case IDs')
    return sorted(result, key=lambda row: str(row['case_id']))


def model_summary(
    rows: list[dict[str, object]], expected: object
) -> dict[str, object]:
    """Count models per attempt and distinguish missing telemetry from drift."""
    requested: Counter[str] = Counter()
    served: Counter[str] = Counter()
    measured = total = 0
    for row in rows:
        attempts = row.get('attempts', [])
        if not isinstance(attempts, list):
            raise ValueError('Invalid attempt journal')
        for attempt in attempts:
            total += 1
            req = attempt.get('requested_models', [])
            actual = attempt.get('served_models', [])
            requested.update(set(req))
            actual = {item for item in actual if item}
            served.update(actual)
            measured += bool(actual)
    return {
        'requested': dict(requested),
        'served': dict(served),
        'attempts': total,
        'attempts_with_served_id': measured,
        'expected_from_tuning': expected,
        'matches_tuning': None if not served else sorted(served) == expected,
    }


def _cache_file(raw: str, cache: Path) -> Path:
    path = Path(raw).resolve()
    if not path.is_relative_to(cache.resolve()) or not path.is_file():
        raise ValueError('Missing trajectory or trajectory outside the cache')
    return path


def _replay_bundle(
    target: Path,
    arm: str,
    rows: list[dict[str, object]],
    cache: Path,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    portable = []
    paths = {}
    for row in rows:
        case = str(row['case_id'])
        record = {key: row[key] for key in ('case_id', 'ranks')}
        record.update(model=row.get('model'), usage=row.get('usage', {}))
        projected = row.get('trajectory')
        if isinstance(projected, str):
            source = _cache_file(projected, cache)
            path = target / arm / 'scoring-trajectories' / f'{case}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
            record['trajectory'] = str(path)
        raw = row.get('canonical_trajectory')
        if isinstance(raw, str):
            source = _cache_file(raw, cache)
            events = trajectory.read_trajectory(source)
            if any(
                event.get('type') == 'final_report'
                and isinstance(event.get('report'), dict)
                for event in events
            ):
                relative = f'trajectories/{arm}/{case}/trajectory.jsonl'
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_replay(path, events)
                paths[case] = relative
        portable.append(record)
    return portable, paths


def _write_replay(path: Path, events: list[dict[str, object]]) -> None:
    # Keep every event while removing only the unavailable local input path.
    for event in events:
        manifest = event.get('manifest')
        if event.get('type') == 'run_start' and isinstance(manifest, dict):
            task = manifest.get('task')
            if isinstance(task, dict):
                task.pop('folder', None)
                runner.atomic_json(
                    path.parent / 'manifest.json', {'task': task}
                )
    path.write_text(
        ''.join(
            json.dumps(event, ensure_ascii=False) + '\n' for event in events
        ),
        encoding='utf-8',
    )


def _representative(
    failure_data: dict[str, object], paths: dict[str, str]
) -> str | None:
    rows = failure_data['cases']
    assert isinstance(rows, list)
    bad = [row for row in rows if row['category'] != 'correct_top1']
    totals = Counter(row['category'] for row in bad)
    for row in sorted(
        bad, key=lambda row: (-totals[row['category']], row['case_id'])
    ):
        if row['case_id'] in paths:
            return paths[row['case_id']]
    return None


def _build(
    root: Path, shared: Path, target: Path, frozen: dict[str, object]
) -> None:
    manifests = frozen['arms']
    assert isinstance(manifests, dict)
    primary = manifests['primary']
    ids = set(primary['heldout_ids'])
    if len(ids) != 60 or ids != set(primary['partition']['heldout']):
        raise ValueError('Publication requires the 60 locked held-out cases')
    tasks = shared / 'agent_tasks/re2-ob'
    answers = shared / 'agent_tasks/heldout.answers.jsonl'
    real = shared / 'runs/phase-06/real'
    all_rows = {}
    scores = {}
    replay_paths: dict[str, dict[str, str]] = {}
    details = {}
    for arm in release.REQUIRED + release.OPTIONAL:
        receipt = root / f'{arm}.complete.json'
        if not receipt.is_file():
            if arm in release.REQUIRED:
                raise ValueError(f'Missing completed arm: {arm}')
            omitted = root / f'{arm}.omitted.json'
            details[arm] = (
                runner.read_object(omitted)
                if omitted.exists()
                else {'status': 'not run'}
            )
            continue
        run = real / 'runs' / f'phase-10-{arm}'
        saved = runner.read_object(receipt)
        if (
            saved['results_sha256'] != release.hash_file(run / 'results.json')
            or runner.read_object(run / 'manifest.json')['frozen']
            != manifests[arm]
        ):
            raise ValueError(f'Saved results differ from the receipt: {arm}')
        expected = (
            {case['case_id'] for case in manifests[arm]['cases']}
            if arm == 'field'
            else ids
        )
        rows = aligned(
            failures.indexed(run / 'results.json').values(), expected
        )
        portable, replay_paths[arm] = _replay_bundle(
            target, arm, rows, real / 'cache'
        )
        output = target / arm
        output.mkdir(exist_ok=True)
        all_rows[arm] = rows
        details[arm] = {
            **saved,
            'usage': reporting.summarize_usage(rows),
            'models': model_summary(rows, frozen['expected_served_models']),
        }
        if arm == 'field':
            reporting.write_predictions(output, portable)
            reporting.field_report(
                output, rows, service.ROOT / 'evalsets/field/gold'
            )
        else:
            scores[arm] = scoring.score_subset(
                answers,
                reporting.write_predictions(output, portable),
                sorted(ids),
                subset_path=output / 'answers-subset.jsonl',
                tasks_dir=tasks,
            )
            (output / 'answers-subset.jsonl').unlink()

    for name in ('baro', 'guarded_baro', 'mmbaro', 'mmbaro_candidates'):
        path = shared / f'runs/phase-02/{name}.heldout.predictions.jsonl'
        if not path.exists():
            continue
        rows = aligned(
            (
                json.loads(line)
                for line in path.read_text().splitlines()
                if line
            ),
            ids,
        )
        all_rows[name] = rows
        output = target / name
        output.mkdir()
        scores[name] = scoring.score_subset(
            answers,
            reporting.write_predictions(output, rows),
            sorted(ids),
            subset_path=output / 'answers-subset.jsonl',
            tasks_dir=tasks,
        )
        (output / 'answers-subset.jsonl').unlink()

    noise_path = (
        service.ROOT / 'results/phase-06/noise-single-gpt-small-5x3.json'
    )
    measurements = runner.read_object(noise_path)
    noise = {
        'measurements': measurements,
        'source': str(noise_path.relative_to(service.ROOT)),
        'sha256': release.hash_file(noise_path),
        'tuning_commit': '8bb180b',
        'note': 'Five tuning cases, three repeats, gpt-small in single mode. '
        'No exact final configuration or held-out noise match is claimed.',
        'same_commit': str(frozen['commit']).startswith('8bb180b'),
    }
    table = []
    difference_rows: list[dict[str, object]] = []
    paired = {}
    for name, score in scores.items():
        row = comparison.table_row(
            name,
            score,
            scores['baro'],
            all_rows[name],
            free=name not in details,
        )
        row['inputs'] = INPUTS[name]
        row['tuning_mean_top1_flip_rate'] = measurements['mean_flip_rate']
        table.append(row)
        if name != 'primary':
            paired[name] = reporting.compare_baseline(
                target / name, scores['primary'], score, name=name
            )
            paired[name]['noise'] = noise
            differences = paired[name]['differences']
            assert isinstance(differences, dict)
            difference_rows.extend(
                {
                    'baseline': name,
                    'metric': metric,
                    'difference': value['value'],
                    'ci_low': value['low'],
                    'ci_high': value['high'],
                    'pairs': value['n_groups'],
                    'tuning_top1_flip_rate': measurements['mean_flip_rate'],
                }
                for metric, value in differences.items()
            )
            runner.atomic_json(
                target / name / 'baseline-comparison.json', paired[name]
            )
    table_output.write(
        target / 'comparison',
        table,
        note='Headline delta is against BARO. Per-arm JSON gives primary minus '
        'each comparator. The flip rate is from Phase 06 tuning repeats.',
    )
    table_output.write(
        target / 'paired-differences',
        difference_rows,
        note='Primary minus baseline. Intervals resample service–fault pairs. '
        'The tuning flip rate describes single-mode service top-1 variability.',
    )
    primary_run = real / 'runs/phase-10-primary'
    # The failure report consumes scored case rows produced by the runner.
    failures.run(primary_run, output=target, final=True)
    representative = _representative(
        runner.read_object(target / 'failure-report.json'),
        replay_paths['primary'],
    )
    cases = {}
    for entry in primary['cases']:
        case_id = entry['case_id']
        folder = tasks / 'cases' / case_id
        if runner.file_hashes(folder) != entry['files']:
            raise ValueError(f'Frozen onset input changed: {case_id}')
        task = runner.read_object(folder / 'task.json')
        cases[case_id] = (folder, float(str(task['inject_time'])))
    runner.atomic_json(target / 'onset.json', onset.evaluate(cases))
    spend = ledger.SpendLedger(
        shared / 'runs/spend.sqlite3', service.ROOT / 'config/budget.yaml'
    ).snapshot()
    runner.atomic_json(
        target / 'comparison.json',
        {
            'rows': table,
            'official': {
                name: dict(score.summary) for name, score in scores.items()
            },
            'paired': paired,
            'noise': noise,
            'arms': details,
            'total_project_spend_usd': spend.spent,
            'held_usd': spend.held,
        },
    )
    runner.atomic_json(
        target / 'replay.json',
        {
            'version': 1,
            'trajectories': sorted(
                path
                for paths in replay_paths.values()
                for path in paths.values()
            ),
            'representative_failure': representative,
        },
    )
    for manifest in manifests.values():
        for item in manifest.get('cases', ()):
            item.pop('folder', None)
    runner.atomic_json(target / 'release.json', frozen)


def publish(root: Path, shared: Path, destination: Path) -> None:
    """Publish a self-contained bundle assembled solely from local evidence."""
    with release.claim(root):
        frozen = runner.read_object(root / 'release.json')
        if release.scoring_inputs(shared) != frozen['scoring_inputs']:
            raise ValueError('Frozen scoring inputs have changed')
        destination = destination.resolve()
        if destination.exists():
            raise ValueError('Publish to a new destination')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
            target = Path(temp) / 'bundle'
            target.mkdir()
            _build(root, shared, target, frozen)
            target.rename(destination)
