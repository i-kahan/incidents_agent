"""Build and validate frozen memory bundles from tuning telemetry only."""

import contextlib
import dataclasses
import datetime
import fcntl
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path

from incidents_agent.agent import state
from incidents_agent.eval import answers
from incidents_agent.eval import runner
from incidents_agent.eval import split
from incidents_agent.eval import telemetry_summary
from incidents_agent.memory import store
from incidents_agent.tools import safety

BUNDLE_VERSION = 1
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _tuning_ids(partition: split.Split) -> tuple[str, ...]:
    ids = partition.tuning
    if (
        not ids
        or len(set(ids)) != len(ids)
        or set(ids) & set(partition.heldout)
        or any(
            not item.strip() or Path(item).name != item or item in ('.', '..')
            for item in ids
        )
    ):
        raise ValueError('Split must contain unique, disjoint, safe case IDs')
    return ids


def _record(
    task: state.Task, answer: answers.Answer, workdir: Path
) -> store.MemoryRecord:
    start = task.fault_start_s
    if start is None or not math.isfinite(start):
        raise ValueError('Seeding requires a finite injection time')
    try:
        stamp = datetime.datetime.fromtimestamp(start, datetime.UTC).isoformat()
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(
            'Injection time is outside the timestamp range'
        ) from error
    payload, _ = telemetry_summary.build_input(task, workdir)
    envelope = payload['evidence']
    if not isinstance(envelope, dict):
        raise ValueError('Invalid telemetry summary')
    evidence = envelope['content']
    if not isinstance(evidence, dict):
        raise ValueError('Invalid telemetry summary content')
    # Labels never enter the symptom text. Redact the whole summary as well
    # as the individual log samples already redacted by the summarizer.
    symptom, _ = safety.redact_secrets_in_text(
        telemetry_summary.encode(evidence)
    )
    observations = []
    components = {answer.service}
    for kind in ('metrics', 'logs'):
        rows = evidence[kind]
        if not isinstance(rows, list):
            raise ValueError('Invalid telemetry observations')
        for row in rows:
            text, _ = safety.redact_secrets_in_text(
                telemetry_summary.encode(row)
            )
            observations.append(f'{kind}: {text}')
            for name in task.candidate_services:
                if re.search(
                    r'(?<![\w-])' + re.escape(name) + r'(?=$|_|[^\w-])',
                    text,
                ):
                    components.add(name)
    return store.MemoryRecord(
        run_id=answer.case_id,
        symptom_summary=symptom,
        affected_components=tuple(sorted(components)),
        diagnosed_root_cause=answer.service,
        fault_type=answer.fault,
        key_evidence=tuple(observations),
        outcome='confirmed',
        created_at=stamp,
        updated_at=stamp,
    )


def seed(
    split_path: Path, tasks_root: Path, answers_path: Path, output: Path
) -> dict[str, object]:
    """Atomically publish a new bundle containing all and only tuning cases.

    Args:
        split_path: Locked split JSON.
        tasks_root: Blind export with a `cases` directory.
        answers_path: Tuning-only answers outside the blind export.
        output: New bundle directory outside the blind export.

    Returns:
        The published provenance manifest.

    Raises:
        ValueError: Inputs violate the tuning-only or publication contract.
        OSError: An input cannot be read or the output cannot be published.
    """
    tasks_root = tasks_root.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output destination already exists')
    if any(
        path.resolve().is_relative_to(tasks_root)
        for path in (answers_path, output)
    ):
        raise ValueError(
            'Answers and output must be outside the blind task view'
        )
    partition = split.from_json(runner.read_object(split_path))
    ids = _tuning_ids(partition)
    answer_hash = answers.file_sha256(answers_path)
    labels = {row.case_id: row for row in answers.load_answers(answers_path)}
    if set(labels) != set(ids):
        raise ValueError('Answers must match tuning case IDs exactly')
    if answers.file_sha256(answers_path) != answer_hash:
        raise ValueError('Answers changed while being read')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix='.memory-seed-', dir=output.parent
    ) as temporary:
        staging = Path(temporary)
        bundle = staging / 'bundle'
        bundle.mkdir()
        memory = store.MemoryStore(bundle / 'memory.sqlite3')
        inputs = {}
        for case_id in ids:
            folder = tasks_root / 'cases' / case_id
            if not folder.resolve().is_relative_to(tasks_root):
                raise ValueError('Case folder escapes the blind task view')
            before = runner.file_hashes(folder)
            task = state.Task.from_task_json(folder / 'task.json')
            if task.case_id != case_id:
                raise ValueError('Task case ID differs from tuning case ID')
            with tempfile.TemporaryDirectory(
                prefix='case-', dir=staging
            ) as workdir:
                memory.save(_record(task, labels[case_id], Path(workdir)))
            if runner.file_hashes(folder) != before:
                raise ValueError('Case inputs changed while seeding')
            inputs[case_id] = before
        manifest: dict[str, object] = {
            'version': BUNDLE_VERSION,
            'frozen': True,
            'summary_policy_version': telemetry_summary.POLICY_VERSION,
            'split_hash': runner.digest(partition.to_json()),
            'tuning_answers_sha256': answer_hash,
            'case_ids': list(ids),
            'inputs': inputs,
            'memory_snapshot_hash': memory.snapshot_hash(),
        }
        runner.atomic_json(bundle / 'manifest.json', manifest)
        validate(manifest, memory, partition)
        # Refuse even an empty pre-existing destination, including one
        # created by another seeder while this one was building.
        with (output.parent / '.memory-seed.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if output.exists() or output.is_symlink():
                raise ValueError('Output destination already exists')
            os.rename(bundle, output)
    return manifest


def validate(
    manifest: dict[str, object],
    memory: store.MemoryStore,
    partition: split.Split,
    selected_ids: Sequence[str] = (),
) -> None:
    """Check bundle provenance, contents and separation from evaluated cases.

    Args:
        manifest: Seed manifest read beside the database.
        memory: Snapshot to check.
        partition: Evaluation's locked split.
        selected_ids: Cases about to be evaluated.

    Raises:
        ValueError: Provenance or contents are inconsistent.
    """
    ids = _tuning_ids(partition)
    if (
        manifest.get('version') != BUNDLE_VERSION
        or manifest.get('frozen') is not True
        or manifest.get('summary_policy_version')
        != telemetry_summary.POLICY_VERSION
        or manifest.get('split_hash') != runner.digest(partition.to_json())
        or manifest.get('case_ids') != list(ids)
        or not _DIGEST.fullmatch(str(manifest.get('tuning_answers_sha256')))
    ):
        raise ValueError('Invalid memory bundle provenance')
    inputs = manifest.get('inputs')
    if not isinstance(inputs, dict) or set(inputs) != set(ids):
        raise ValueError('Invalid memory bundle input provenance')
    for files in inputs.values():
        if (
            not isinstance(files, dict)
            or 'task.json' not in files
            or any(
                not isinstance(name, str)
                or Path(name).is_absolute()
                or '..' in Path(name).parts
                or not _DIGEST.fullmatch(str(digest))
                for name, digest in files.items()
            )
        ):
            raise ValueError('Invalid memory bundle input hashes')
    if set(ids) & set(selected_ids):
        raise ValueError('Evaluation cases overlap seeded tuning cases')
    records = memory.list_all()
    if {record.run_id for record in records} != set(ids) or any(
        record.outcome != 'confirmed'
        or not record.diagnosed_root_cause
        or not record.fault_type
        for record in records
    ):
        raise ValueError(
            'Memory must contain exactly the confirmed tuning cases'
        )
    if memory.snapshot_hash() != manifest.get('memory_snapshot_hash'):
        raise ValueError('Memory snapshot hash differs from its manifest')


@dataclasses.dataclass(frozen=True)
class FrozenSnapshot:
    """Private SQLite copy with ownership of its temporary directory."""

    memory: store.MemoryStore
    manifest: dict[str, object]
    temporary: tempfile.TemporaryDirectory[str]

    @property
    def digest(self) -> str:
        """Return the validated content identity."""
        return str(self.manifest['memory_snapshot_hash'])


def capture(
    bundle: Path, partition: split.Split, selected_ids: Sequence[str]
) -> FrozenSnapshot:
    """Capture and validate a private SQLite snapshot before model dispatch.

    Args:
        bundle: Directory produced by `seed`.
        partition: Locked evaluation split.
        selected_ids: Evaluation cases, disjoint from seed cases.

    Returns:
        A frozen copy whose lifetime is owned by the returned object.

    Raises:
        ValueError: The bundle is invalid or leaks evaluated cases.
        OSError: The bundle cannot be read.
    """
    manifest = runner.read_object(bundle / 'manifest.json')
    temporary = tempfile.TemporaryDirectory(prefix='incident-memory-')
    destination = Path(temporary.name) / 'memory.sqlite3'
    try:
        source = (bundle / 'memory.sqlite3').resolve()
        with (
            contextlib.closing(
                sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)
            ) as original,
            contextlib.closing(sqlite3.connect(destination)) as copied,
        ):
            original.backup(copied)
        memory = store.MemoryStore(destination, frozen=True)
        validate(manifest, memory, partition, selected_ids)
        destination.chmod(0o400)
        return FrozenSnapshot(memory, manifest, temporary)
    except sqlite3.Error as error:
        temporary.cleanup()
        raise ValueError('Invalid memory snapshot database') from error
    except (ValueError, OSError):
        temporary.cleanup()
        raise
