"""Deterministic blind telemetry summaries shared by evaluation and seeding."""

import json
import math
from pathlib import Path

from incidents_agent import ingest
from incidents_agent.agent import results
from incidents_agent.agent import state
from incidents_agent.tools import logs
from incidents_agent.tools import metrics
from incidents_agent.tools import safety
from incidents_agent.tools import tables

POLICY_VERSION = 1


def encode(value: object) -> str:
    """Serialize a deterministic model input without incidental whitespace."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def build_input(
    task: state.Task, workdir: Path
) -> tuple[dict[str, object], int]:
    """Summarize blind inputs; return the payload and preprocessing count."""
    if task.fault_start_s is None or not math.isfinite(task.fault_start_s):
        raise ValueError(
            'Single-call baseline requires a finite fault start time'
        )
    shifted: list[dict[str, object]] = []
    patterns: list[dict[str, object]] = []
    unavailable: list[dict[str, object]] = []
    calls = 0
    ingested = ingest.ingest_folder(task.folder, workdir=workdir)
    try:
        ctx = tables.ToolContext(ingested.connection, ingested.catalog)
        for table in sorted(
            ingested.catalog.tables, key=lambda item: item.name
        ):
            args: dict[str, object] = {
                'table': table.name,
                'split_time': task.fault_start_s,
                'top_k': 15,
            }
            calls += 1
            compared = metrics.compare_windows(ctx, args)
            ranked = compared.get('ranked')
            if compared.get('ok') and isinstance(ranked, list):
                shifted.extend({**row, 'table': table.name} for row in ranked)
            else:
                unavailable.append(
                    {
                        'table': table.name,
                        'kind': 'metrics',
                        'error': compared.get('error'),
                    }
                )
            calls += 1
            searched = logs.search_text(
                ctx,
                {**args, 'pattern': '.*', 'top_k': 20},
                new_only=True,
            )
            matches = results.payload(searched).get('content', {})
            if searched.get('ok') and isinstance(matches, dict):
                for row in matches.get('matches', []):
                    cleaned, _ = safety.redact_secrets(row)
                    if not isinstance(cleaned, dict):
                        raise ValueError('Invalid log pattern')
                    patterns.append(
                        {
                            **cleaned,
                            'table': table.name,
                            'sample_text': str(cleaned['sample_text'])[:320],
                        }
                    )
            else:
                unavailable.append(
                    {
                        'table': table.name,
                        'kind': 'logs',
                        'error': searched.get('error'),
                    }
                )
    finally:
        ingested.close()
    shifted.sort(
        key=lambda row: (
            -float(str(row['score'])),
            str(row['table']),
            str(row['column']),
        )
    )
    patterns.sort(
        key=lambda row: (
            -int(str(row['count_after'])),
            str(row['table']),
            str(row['pattern_template']),
        )
    )
    evidence: dict[str, object] = {
        'metrics': shifted[:15],
        'logs': patterns[:20],
        'unavailable': unavailable,
        'truncated': len(shifted) > 15 or len(patterns) > 20,
        'logs_available': bool(patterns),
    }
    # Drop lowest-ranked evidence first, with a fixed deterministic policy.
    while len(encode(evidence).encode()) > 16384:
        evidence['truncated'] = True
        buckets = [evidence[key] for key in ('logs', 'metrics', 'unavailable')]
        bucket = next(
            (item for item in buckets if isinstance(item, list) and item), None
        )
        if not isinstance(bucket, list):
            raise ValueError('Evidence metadata exceeds the summary cap')
        bucket.pop()
    return {
        'candidate_services': list(task.candidate_services),
        'candidate_indicators': list(task.candidate_indicators),
        'fault_start_s': task.fault_start_s,
        'evidence': safety.wrap_untrusted_data(evidence),
    }, calls
