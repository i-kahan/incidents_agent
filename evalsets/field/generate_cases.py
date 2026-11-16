"""Generate deterministic Phase 03 field-set fixture cases."""

import csv
import json
import pathlib
import random

_BASE_DIR = pathlib.Path(__file__).resolve().parent


def _write_csv(
    path: pathlib.Path, headers: list[str], rows: list[list[object]]
) -> None:
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def generate_config_diff(root: pathlib.Path) -> None:
    """Create the config_diff case where a patch breaks auth JWKS fetches."""
    rng = random.Random(301)
    case_dir = root / 'cases' / 'config_diff'
    gold_dir = root / 'gold'
    case_dir.mkdir(parents=True, exist_ok=True)
    gold_dir.mkdir(parents=True, exist_ok=True)

    t0 = 1_700_000_000
    split_t = 1_700_000_200
    metric_rows: list[list[object]] = []
    for step in range(40):
        ts = t0 + step * 10
        after = ts > split_t
        gw_lat = (
            180.0 + rng.uniform(-10.0, 10.0)
            if after
            else 18.0 + rng.uniform(-2.0, 2.0)
        )
        gw_err = (
            0.42 + rng.uniform(-0.02, 0.02)
            if after
            else rng.uniform(0.0, 0.002)
        )
        auth_err = (
            0.91 + rng.uniform(-0.02, 0.02)
            if after
            else rng.uniform(0.0, 0.001)
        )
        auth_p99 = (
            6.5 + rng.uniform(-0.4, 0.4)
            if after
            else 4.8 + rng.uniform(-0.3, 0.3)
        )
        auth_cpu = 14.0 + rng.uniform(-1.5, 1.5)
        cache_cpu = 22.0 + rng.uniform(-1.0, 1.0)
        metric_rows.append(
            [
                ts,
                round(gw_lat, 3),
                round(gw_err, 4),
                round(auth_err, 4),
                round(auth_p99, 3),
                round(auth_cpu, 2),
                round(cache_cpu, 2),
            ]
        )
    _write_csv(
        case_dir / 'metrics.csv',
        [
            'time',
            'gateway_latency_ms',
            'gateway_error_rate',
            'auth_service_error_rate',
            'auth_service_p99_ms',
            'auth_service_cpu',
            'cache_cpu',
        ],
        metric_rows,
    )

    log_lines = [
        f'{t0 + 10} INFO auth-service JWKS cache refreshed in 42ms',
        f'{t0 + 60} INFO gateway Request GET /orders 200 OK',
        f'{t0 + 150} INFO auth-service Token verification healthy',
    ]
    for step in range(12):
        ts = split_t + 5 + step * 10
        log_lines.append(
            f'{ts} ERROR auth-service JWKS fetch timed out after 5ms '
            f'req_id=req-{1000 + step}'
        )
        log_lines.append(
            f'{ts + 1} ERROR gateway Upstream auth-service returned 503 '
            f'req_id=req-{1000 + step}'
        )
    (case_dir / 'app.log').write_text(
        '\n'.join(log_lines) + '\n', encoding='utf-8'
    )

    patch_text = (
        'diff --git a/deploy/auth-service.yaml b/deploy/auth-service.yaml\n'
        '--- a/deploy/auth-service.yaml\n'
        '+++ b/deploy/auth-service.yaml\n'
        '@@ -18,3 +18,3 @@ env:\n'
        '   AUTH_JWKS_CACHE_TTL_S: 60\n'
        '-  AUTH_JWKS_TIMEOUT_MS: 2000\n'
        '+  AUTH_JWKS_TIMEOUT_MS: 5\n'
        '   AUTH_LOG_LEVEL: INFO\n'
    )
    (case_dir / 'deploy_diff.patch').write_text(patch_text, encoding='utf-8')

    runbook = (
        '# Authentication Service Runbook\n\n'
        '## Overview\n\n'
        'The `auth-service` validates JWT signatures using keys fetched from\n'
        'the identity provider JWKS endpoint.\n\n'
        '## JWKS Timeout Errors\n\n'
        'If `AUTH_JWKS_TIMEOUT_MS` is set below 50ms, key refresh requests\n'
        'fail before TLS handshakes complete, causing `auth-service` to\n'
        'reject valid tokens while CPU remains low. Restore the timeout to\n'
        '2000ms to mitigate.\n'
    )
    (case_dir / 'runbook.md').write_text(runbook, encoding='utf-8')

    gold = {
        'case_id': 'config_diff',
        'root_cause_component': 'auth-service',
        'accepted_aliases': ['auth-service', 'auth_service', 'auth'],
        'fault_category': 'bad_config_change',
        'onset_time_s': split_t,
        'required_evidence': [
            'AUTH_JWKS_TIMEOUT_MS',
            'deploy_diff.patch',
            'JWKS fetch timed out',
        ],
    }
    (gold_dir / 'config_diff.json').write_text(
        json.dumps(gold, indent=2) + '\n', encoding='utf-8'
    )


def generate_victim_vs_cause(root: pathlib.Path) -> None:
    """Create victim_vs_cause where api-gateway is the loudest symptom."""
    rng = random.Random(302)
    case_dir = root / 'cases' / 'victim_vs_cause'
    gold_dir = root / 'gold'
    case_dir.mkdir(parents=True, exist_ok=True)
    gold_dir.mkdir(parents=True, exist_ok=True)

    t0 = 1_700_001_000
    split_t = 1_700_001_200
    metric_rows: list[list[object]] = []
    for step in range(40):
        ts = t0 + step * 10
        after = ts > split_t
        # api-gateway queue amplifies dramatically due to retries (loudest).
        gw_queue = (
            620.0 + rng.uniform(-8.0, 8.0)
            if after
            else 4.0 + rng.uniform(-0.1, 0.1)
        )
        gw_lat = (
            950.0 + rng.uniform(-15.0, 15.0)
            if after
            else 25.0 + rng.uniform(-0.5, 0.5)
        )
        order_lat = (
            420.0 + rng.uniform(-20.0, 20.0)
            if after
            else 15.0 + rng.uniform(-1.5, 1.5)
        )
        db_lock_wait = (
            180.0 + rng.uniform(-12.0, 12.0)
            if after
            else 8.0 + rng.uniform(-2.5, 2.5)
        )
        db_conns = 100.0 if after else 24.0 + rng.uniform(-4.0, 4.0)
        metric_rows.append(
            [
                ts,
                round(gw_queue, 2),
                round(gw_lat, 2),
                round(order_lat, 2),
                round(db_lock_wait, 2),
                round(db_conns, 2),
            ]
        )
    _write_csv(
        case_dir / 'metrics.csv',
        [
            'time',
            'api_gateway_queue_depth',
            'api_gateway_latency_ms',
            'order_service_latency_ms',
            'inventory_db_lock_wait_ms',
            'inventory_db_active_connections',
        ],
        metric_rows,
    )

    trace_rows: list[list[object]] = []
    for idx in range(30):
        ts = t0 + idx * 12
        after = ts > split_t
        root_id = f'root-{idx:03d}'
        order_id = f'ord-{idx:03d}'
        db_id = f'db-{idx:03d}'
        cache_id = f'cache-{idx:03d}'
        status = 'error' if after else 'ok'
        db_dur = 480.0 if after else 3.5
        order_dur = 520.0 if after else 14.0
        gw_dur = 980.0 if after else 24.0
        trace_rows.append(
            [ts * 1000, root_id, '', 'api-gateway', gw_dur, status]
        )
        trace_rows.append(
            [ts * 1000, order_id, root_id, 'order-service', order_dur, status]
        )
        trace_rows.append(
            [ts * 1000, cache_id, order_id, 'cache-proxy', 2.0, 'ok']
        )
        trace_rows.append(
            [ts * 1000, db_id, order_id, 'inventory-db', db_dur, status]
        )
    _write_csv(
        case_dir / 'traces.csv',
        [
            'startTimeMillis',
            'spanID',
            'parentSpanID',
            'serviceName',
            'duration',
            'statusCode',
        ],
        trace_rows,
    )

    log_lines = [
        f'{t0 + 12} INFO inventory-db Connection pool usage 18/100',
        f'{t0 + 40} INFO api-gateway Request completed in 24ms',
    ]
    for step in range(8):
        ts = split_t + 4 + step * 15
        log_lines.append(
            f'{ts} ERROR inventory-db Lock wait timeout; pool 100/100 '
            f'tx_id=tx-{200 + step}'
        )
        log_lines.append(
            f'{ts + 1} WARN order-service Retrying inventory-db read '
            f'tx_id=tx-{200 + step}'
        )
        log_lines.append(
            f'{ts + 2} ERROR api-gateway Request queue saturated after retry '
            f'tx_id=tx-{200 + step}'
        )
    (case_dir / 'service.log').write_text(
        '\n'.join(log_lines) + '\n', encoding='utf-8'
    )

    arch = (
        '# Service Dependency Architecture\n\n'
        '## Call Chain\n\n'
        'Requests flow from `api-gateway` -> `order-service` -> '
        '`inventory-db` (with optional `cache-proxy` lookups).\n\n'
        '## Retry Amplification\n\n'
        'Because `api-gateway` holds open client retries while waiting on\n'
        '`order-service`, downstream stalls in `inventory-db` inflate\n'
        '`api_gateway_queue_depth` most visibly even though `api-gateway`\n'
        'is only a victim.\n'
    )
    (case_dir / 'architecture.md').write_text(arch, encoding='utf-8')

    gold = {
        'case_id': 'victim_vs_cause',
        'root_cause_component': 'inventory-db',
        'accepted_aliases': ['inventory-db', 'inventory_db', 'inventorydb'],
        'victim_component': 'api-gateway',
        'fault_category': 'downstream_saturation',
        'onset_time_s': split_t,
        'required_evidence': [
            'inventory-db',
            'Lock wait timeout',
            'traces.csv',
        ],
    }
    (gold_dir / 'victim_vs_cause.json').write_text(
        json.dumps(gold, indent=2) + '\n', encoding='utf-8'
    )


def main() -> None:
    """Generate both Phase 03 field-set fixture folders deterministically."""
    generate_config_diff(_BASE_DIR)
    generate_victim_vs_cause(_BASE_DIR)


if __name__ == '__main__':
    main()
