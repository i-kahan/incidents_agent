"""Frozen evaluation manifests, process-safe caching and bounded retries."""

import dataclasses
import fcntl
import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent import futures
from datetime import UTC
from datetime import datetime
from pathlib import Path

from incidents_agent.eval import retry
from incidents_agent.llm import contracts


@dataclasses.dataclass(frozen=True)
class Case:
    """One blind input folder; evaluator labels are deliberately absent."""

    case_id: str
    folder: Path
    suite: str = 'rcaeval'


def digest(value: object) -> str:
    """Hash a canonical JSON value, independent of mapping insertion order."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(',', ':'), default=str
        ).encode()
    ).hexdigest()


def file_hashes(root: Path) -> dict[str, str]:
    """Fingerprint all regular inputs by relative name and contents."""
    if not root.is_dir():
        raise ValueError(f'Input folder is missing: {root}')
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Input symlinks are not supported')
        if path.is_file():
            files[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    if not files:
        raise ValueError('Input folder is empty')
    return files


def atomic_json(path: Path, value: object) -> None:
    """Replace one complete durable JSON artifact atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, indent=2, default=str)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_object(path: Path) -> dict[str, object]:
    """Read a JSON object without silently accepting another top-level type."""
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return value


def freeze(
    cases: Sequence[Case],
    configuration: Mapping[str, object],
    *,
    commit: str,
    dry_run: bool,
    repeat_id: str = 'default',
) -> dict[str, object]:
    """Freeze selected cases and all output-affecting configuration."""
    configuration = json.loads(json.dumps(configuration, default=str))
    ids = [case.case_id for case in cases]
    if not cases or len(set(ids)) != len(ids) or not repeat_id:
        raise ValueError('Cases must be nonempty/unique; repeat_id is required')
    suites = {case.suite for case in cases}
    if len(suites) != 1 or not suites <= {'field', 'rcaeval'}:
        raise ValueError('Run exactly one recognized suite at a time')
    return {
        'version': 1,
        'configuration': dict(configuration),
        'configuration_fingerprint': digest(configuration),
        'git_commit': commit,
        'dry_run': dry_run,
        'repeat_id': repeat_id,
        'cases': [
            {
                'case_id': case.case_id,
                'folder': str(case.folder.resolve()),
                'suite': case.suite,
                'files': file_hashes(case.folder),
            }
            for case in cases
        ],
    }


def cache_key(
    manifest: Mapping[str, object], case: Mapping[str, object]
) -> str:
    """Exclude reporting/commit/path changes, include repeats and fake mode."""
    return digest(
        {
            'configuration': manifest['configuration_fingerprint'],
            'repeat_id': manifest['repeat_id'],
            'dry_run': manifest['dry_run'],
            'case_id': case['case_id'],
            'suite': case['suite'],
            'files': case['files'],
        }
    )


Execute = Callable[[Case, Path, str], dict[str, object]]


def _cached_case(
    case: Case,
    frozen: Mapping[str, object],
    manifest: Mapping[str, object],
    namespace: Path,
    execute: Execute,
) -> dict[str, object]:
    key = cache_key(manifest, frozen)
    folder = namespace / 'cache' / key
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'claim.lock').open('a+b') as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        if file_hashes(case.folder) != frozen['files']:
            raise ValueError(
                'Case inputs changed after the manifest was frozen'
            )
        completed = folder / 'result.json'
        if completed.exists():
            return read_object(completed)
        attempts_path = folder / 'attempts.json'
        attempts_record = (
            read_object(attempts_path)
            if attempts_path.exists()
            else {'attempts': []}
        )
        raw_attempts = attempts_record.get('attempts')
        if not isinstance(raw_attempts, list):
            raise ValueError('Invalid cache attempt journal')
        attempts: list[dict[str, object]] = raw_attempts
        terminal: dict[str, object] | None = None
        configuration = manifest.get('configuration')
        settings = (
            configuration.get('agent')
            if isinstance(configuration, dict)
            else None
        )
        primary = (
            str(settings.get('model_alias', ''))
            if isinstance(settings, dict)
            else ''
        )
        strategy = retry.PrimaryOnly()

        def invoke(entry: dict[str, object]) -> dict[str, object] | None:
            run_id = str(entry['run_id'])
            attempt_dir = folder / 'attempts' / run_id
            try:
                output = execute(case, folder / 'attempts', run_id)
            except (
                retry.AttemptFailure,
                contracts.BudgetRefused,
                contracts.BillingUncertain,
                contracts.ModelUnavailable,
                contracts.ProviderError,
                contracts.RetryDeferred,
            ) as error:
                usage_path = attempt_dir / 'usage.json'
                usage = read_object(usage_path) if usage_path.exists() else {}
                held = bool(usage.get('held_usd'))
                failure = (
                    error.failure
                    if isinstance(error, retry.AttemptFailure)
                    else retry.classify(error, held=held)
                )
                if held:
                    failure = dataclasses.replace(
                        failure,
                        category='billing_reconciliation_required',
                        retryable=False,
                        billing='unknown',
                        delay_s=0,
                    )
                entry.update(
                    status='failed',
                    failure=failure.to_dict(),
                    error=failure.error,
                    retry_at=time.time() + failure.delay_s,
                )
                atomic_json(attempts_path, {'attempts': attempts})
                return None
            # Persistence failures must abort, never cause another paid call.
            atomic_json(attempt_dir / 'execution.json', output)
            entry.update(
                status='complete',
                requested_models=output.get('requested_models', []),
                served_models=output.get('served_models', []),
            )
            atomic_json(attempts_path, {'attempts': attempts})
            return output

        if attempts:
            last = attempts[-1]
            attempt_dir = folder / 'attempts' / str(last['run_id'])
            completed_attempt = attempt_dir / 'execution.json'
            if completed_attempt.exists():
                terminal = read_object(completed_attempt)
            elif last.get('status') == 'running':
                if any(
                    (attempt_dir / name).exists()
                    for name in (
                        'report.json',
                        'response.json',
                    )
                ):
                    terminal = invoke(last)
                elif manifest['dry_run'] is True:
                    last.update(
                        status='failed',
                        failure=retry.Failure(
                            'interrupted_dry_attempt',
                            'interrupted',
                            True,
                            billing='unbilled',
                        ).to_dict(),
                    )
                else:
                    last.update(
                        status='blocked',
                        error='interrupted_paid_attempt',
                    )
            if terminal is None and last.get('status') == 'blocked':
                terminal = {
                    'ranks': [],
                    'error': last.get('error', 'blocked_attempt'),
                    'stop_reason': 'billing_reconciliation_required',
                }

        while terminal is None:
            if attempts:
                last = attempts[-1]
                raw = last.get('failure')
                if not isinstance(raw, dict):
                    raise ValueError(
                        'Attempt journal lacks a classified failure'
                    )
                failure = retry.Failure(
                    str(raw['category']),
                    str(raw['error']),
                    raw.get('retryable') is True,
                    float(str(raw.get('delay_s', 0))),
                    str(raw.get('billing', 'unknown')),
                )
                if strategy.next_model(primary, failure, attempts) is None:
                    terminal = {
                        'ranks': [],
                        'error': failure.error,
                        'failure': failure.to_dict(),
                        'stop_reason': failure.category,
                        'retry_exhausted': failure.retryable
                        and len(attempts) >= retry.CASE_ATTEMPTS,
                    }
                    break
                delay = max(
                    0, float(str(last.get('retry_at', 0))) - time.time()
                )
                time.sleep(min(delay, retry.WINDOW_S))
            run_id = f'{key[:24]}-attempt-{len(attempts) + 1}'
            entry: dict[str, object] = {
                'run_id': run_id,
                'status': 'running',
                'primary_model': primary,
            }
            attempts.append(entry)
            atomic_json(attempts_path, {'attempts': attempts})
            terminal = invoke(entry)
        ranks = terminal.get('ranks')
        if not isinstance(ranks, list) or not all(
            isinstance(rank, str) for rank in ranks
        ):
            raise ValueError('Executor returned invalid ranks')
        total_usage: dict[str, object] = {}
        for attempt in attempts:
            usage_path = (
                folder / 'attempts' / str(attempt['run_id']) / 'usage.json'
            )
            if usage_path.exists():
                usage = read_object(usage_path)
                attempt['usage'] = usage
                for name in (
                    'input_tokens',
                    'output_tokens',
                    'cost_usd',
                    'wall_time_s',
                    'tool_calls',
                    'held_usd',
                    'model_turns',
                    'preprocessing_calls',
                    'preprocessing_time_s',
                ):
                    value = usage.get(name)
                    if isinstance(value, (int, float)):
                        total_usage[name] = (
                            float(str(total_usage.get(name, 0))) + value
                        )
        last_usage = terminal.get('usage')
        if total_usage:
            terminal['usage'] = {
                **(last_usage if isinstance(last_usage, dict) else {}),
                **total_usage,
            }
        result = {
            **terminal,
            'case_id': case.case_id,
            'cache_key': key,
            'attempts': attempts,
        }
        atomic_json(attempts_path, {'attempts': attempts})
        atomic_json(completed, result)
        return result


def run(
    cases: Sequence[Case],
    manifest: Mapping[str, object],
    *,
    output_root: Path,
    execute: Execute,
    workers: int = 1,
    resume: Path | None = None,
) -> tuple[Path, list[dict[str, object]]]:
    """Run one frozen configuration; caches and manifests isolate fake data."""
    if workers < 1:
        raise ValueError('workers must be positive')
    frozen = manifest.get('cases')
    if not isinstance(frozen, list) or len(frozen) != len(cases):
        raise ValueError('Manifest cases do not match execution inputs')
    for case, entry in zip(cases, frozen, strict=True):
        if not isinstance(entry, dict) or (
            case.case_id,
            str(case.folder.resolve()),
            case.suite,
        ) != (
            entry.get('case_id'),
            entry.get('folder'),
            entry.get('suite'),
        ):
            raise ValueError('Manifest cases do not match execution inputs')
    mode = 'dry' if manifest['dry_run'] else 'real'
    namespace = output_root.resolve() / mode
    if resume is not None:
        folder = resume.resolve()
        if not folder.is_relative_to(namespace / 'runs'):
            raise ValueError('Resume must belong to this output namespace')
        stored = read_object(folder / 'manifest.json')
        if stored.get('frozen') != manifest:
            raise ValueError('Resume configuration, inputs or commit changed')
    else:
        folder = namespace / 'runs' / uuid.uuid4().hex
        atomic_json(
            folder / 'manifest.json',
            {
                'created_at': datetime.now(UTC).isoformat(),
                'frozen': dict(manifest),
            },
        )

    def one(index: int) -> dict[str, object]:
        return _cached_case(
            cases[index], frozen[index], manifest, namespace, execute
        )

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, range(len(cases))))
    atomic_json(folder / 'results.json', {'cases': results})
    return folder, results
