"""Offline, observable failure categories over every intended benchmark case."""

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from incidents_agent.agent import trajectory
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import split
from incidents_agent.eval import table_output


def indexed(path: Path) -> dict[str, dict[str, object]]:
    """Read a case collection, rejecting duplicates and malformed rows."""
    rows = runner.read_object(path).get('cases')
    if not isinstance(rows, list):
        raise ValueError(f'Expected cases in {path}')
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('case_id'), str):
            raise ValueError('Malformed case record')
        case_id = row['case_id']
        if case_id in found:
            raise ValueError('Duplicate case record')
        found[case_id] = row
    return found


def category(row: Mapping[str, object]) -> str:
    """Assign one primary category, with execution failures taking priority."""
    if row.get('stop_reason') in (
        'billing_reconciliation_required',
        'billing_or_budget_refused',
        'budget_refused',
        'spend_refused',
        'reservation_refused',
    ):
        return 'execution_blocked'
    if row.get('error'):
        return 'execution_error'
    if row.get('service_rank') == 1:
        return 'correct_top1'
    if not row.get('has_ranking'):
        return 'no_ranking'
    inspected = row.get('inspected_root')
    if inspected is False:
        return 'wrong_root_uninspected'
    if inspected is True:
        return 'wrong_root_inspected'
    return 'wrong_inspection_unknown'


def step_counts(
    folder: Path, record: Mapping[str, object]
) -> dict[str, object]:
    """Count canonical events across attempts, retaining measured fallbacks."""
    usage = record.get('usage')
    counts = {
        key: usage.get(key) if isinstance(usage, dict) else None
        for key in ('model_turns', 'tool_calls')
    }
    attempts = record.get('attempts')
    if not isinstance(attempts, list) or not attempts:
        return counts
    sums = {'model_turns': 0, 'tool_calls': 0}
    for attempt in attempts:
        path = (
            folder.parent.parent
            / 'cache'
            / str(record['cache_key'])
            / 'attempts'
            / str(attempt['run_id'])
            / 'trajectory.jsonl'
        )
        if not path.exists():
            return counts
        events = trajectory.read_trajectory(path)
        sums['model_turns'] += sum(
            e.get('type') == 'model_call' for e in events
        )
        sums['tool_calls'] += sum(e.get('type') == 'tool_call' for e in events)
    return dict(sums)


def forced(record: Mapping[str, object]) -> bool | None:
    """Read an explicit fact; never infer forced finalization from low rank."""
    value = record.get('forced')
    if type(value) is bool:
        return value
    report = record.get('report')
    if isinstance(report, dict):
        causes = report.get('causes', [])
        if isinstance(causes, list) and any(
            str(reason).startswith('forced finalisation:')
            for cause in causes
            if isinstance(cause, dict)
            for reason in cause.get('confidence_reasons', [])
        ):
            return True
    canonical = record.get('canonical_trajectory')
    if isinstance(canonical, str):
        checkpoint = Path(canonical).parent / 'checkpoint.json'
        if checkpoint.exists():
            data = runner.read_object(checkpoint)
            if 'submission' in data:
                return data['submission'] is None
    return None


def run(
    folder: Path,
    *,
    output: Path | None = None,
    final: bool = False,
    split_path: Path = service.ROOT / 'splits/re2-ob.json',
) -> tuple[Path, str]:
    """Validate saved identities and produce a report without provider calls."""
    frozen = runner.read_object(folder / 'manifest.json').get('frozen')
    if not isinstance(frozen, dict) or not isinstance(
        frozen.get('cases'), list
    ):
        raise ValueError('Invalid run manifest')
    entries = frozen['cases']
    ids = [entry['case_id'] for entry in entries]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('Manifest case IDs must be nonempty and unique')
    if any(entry.get('suite') != 'rcaeval' for entry in entries):
        raise ValueError('Failure reporting supports RCAEval only')
    raw_split = frozen.get('partition')
    if not isinstance(raw_split, dict):
        raw_split = runner.read_object(split_path)
    partition = split.from_json(raw_split)
    configuration = frozen.get('configuration')
    if not isinstance(configuration, dict) or configuration.get(
        'split_hash'
    ) != runner.digest(partition.to_json()):
        raise ValueError('Run and split fingerprints differ')
    if set(ids) - set(partition.tuning + partition.heldout):
        raise ValueError('Manifest has foreign case IDs')
    if not final and set(ids).intersection(partition.heldout):
        raise ValueError('Held-out failure reports require --final')
    scores = indexed(folder / 'case-scores.json')
    records = indexed(folder / 'results.json')
    if set(scores) != set(ids) or set(records) - set(ids):
        raise ValueError('Saved scores/results differ from intended cases')
    rows = []
    for case_id in ids:
        record = records.get(case_id, {'error': 'missing_result'})
        score = scores[case_id]
        usage = record.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        attempts = record.get('attempts')
        row = {
            'case_id': case_id,
            'fault': score['fault'],
            'service_rank': score['service_rank'],
            'indicator_rank': score['indicator_rank'],
            **step_counts(folder, record),
            'inspected_root': score.get('inspected_root'),
            'forced': forced(record),
            'outcome': usage.get('outcome'),
            'stop_reason': record.get('stop_reason', usage.get('stop_reason')),
            'attempts': len(attempts) if isinstance(attempts, list) else 0,
            'error': record.get('error'),
            'has_ranking': bool(record.get('ranks')),
        }
        row['category'] = category(row)
        rows.append(row)
    summary = {
        'n_cases': len(ids),
        'dry_run': frozen['dry_run'],
        'counts': dict(Counter(str(row['category']) for row in rows)),
        'outcomes': dict(Counter(str(row['outcome']) for row in rows)),
        'stop_reasons': dict(Counter(str(row['stop_reason']) for row in rows)),
        'forced': dict(Counter(str(row['forced']) for row in rows)),
        'cases': rows,
    }
    target = output or folder
    target.mkdir(parents=True, exist_ok=True)
    runner.atomic_json(target / 'failure-report.json', summary)
    note = (
        'Categories describe observed execution and service top-1 misses; '
        'inspection does not establish diagnosis quality.\n\n'
        + json.dumps(
            {key: value for key, value in summary.items() if key != 'cases'},
            indent=2,
        )
    )
    markdown = table_output.write(target / 'failure-report', rows, note=note)
    return target, markdown
