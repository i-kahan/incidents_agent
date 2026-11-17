"""Continue a finished run after the user adds files.

The added files are ingested together with the earlier ones, the ledger
and every earlier result are carried over (so old citations stay valid),
a check that ended inconclusive is opened again so verify can rerun on it,
the model gets a "what changed" briefing, and the run continues under a
fresh per-continuation budget. The result is the same run with the next
continuation index.
"""

import dataclasses
import json
import os
import shutil
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from incidents_agent import ingest
from incidents_agent.agent import config
from incidents_agent.agent import context
from incidents_agent.agent import graph
from incidents_agent.agent import ledger
from incidents_agent.agent import memory_record
from incidents_agent.agent import prompts
from incidents_agent.agent import results
from incidents_agent.agent import run_state
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.llm import contracts
from incidents_agent.memory import store as incident_memory


def _link_tree(source: Path, target: Path) -> None:
    for path in sorted(source.rglob('*')):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file() and path.suffix not in ('.duckdb', '.wal'):
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, destination)
            except OSError:
                shutil.copy2(path, destination)


def _combine(folder: Path, added: Sequence[Path], target: Path) -> list[str]:
    """Build the folder for this continuation; return the added names."""
    if target.exists():
        raise FileExistsError(f'{target} already exists')
    _link_tree(folder, target)
    names: list[str] = []
    for path in added:
        destination = target / path.name
        if destination.exists():
            shutil.rmtree(target)
            raise ValueError(
                f'{path.name} is already in the run; rename the added file'
            )
        if path.is_dir():
            shutil.copytree(path, destination)
        elif path.is_file():
            shutil.copy2(path, destination)
        else:
            shutil.rmtree(target)
            raise FileNotFoundError(f'Added file not found: {path}')
        names.append(path.name)
    return names


def what_changed(
    before: Mapping[str, object], after: Mapping[str, object]
) -> list[str]:
    """Describe new and changed tables and documents between two catalogs.

    Args:
        before: Catalog summary from the earlier pass.
        after: Catalog summary after the added files were ingested.

    Returns:
        One line per change, in a stable order.
    """
    old_tables = before.get('tables')
    new_tables = after.get('tables')
    old = old_tables if isinstance(old_tables, dict) else {}
    new = new_tables if isinstance(new_tables, dict) else {}
    lines: list[str] = []
    for name in sorted(new):
        info = new[name]
        if name not in old:
            lines.append(
                f'- New table {name} from {info["source_file"]}: '
                f'{info["row_count"]} rows, {info["columns"]} columns'
            )
        elif old[name] != info:
            lines.append(
                f'- Table {name} changed: {old[name]["row_count"]} -> '
                f'{info["row_count"]} rows'
            )
    old_docs = before.get('documents')
    new_docs = after.get('documents')
    seen = set(old_docs) if isinstance(old_docs, list) else set()
    for doc_id in new_docs if isinstance(new_docs, list) else []:
        if doc_id not in seen:
            lines.append(f'- New document {doc_id}')
    return lines or ['- No new tables or documents were found in the files.']


def continue_run(
    run_dir: Path,
    added: Sequence[Path],
    client: contracts.ModelClient,
    agent_config: config.AgentConfig | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    memory_store: incident_memory.MemoryStore | None = None,
    memory_path: Path | None = None,
    ingest_config: ingest_schema.IngestConfig | None = None,
) -> graph.RunResult:
    """Continue a finished run with added files.

    Args:
        run_dir: Folder of the run, holding its `checkpoint.json`.
        added: Files or folders to add.
        client: Model client.
        agent_config: Configuration for this pass; the run's own when None.
        clock: Monotonic clock, injectable for tests.
        memory_store: Optional pre-opened incident memory store.
        memory_path: Optional path to the incident memory SQLite file.
        ingest_config: Optional ingest settings, such as the user's
            per-file classification overrides from the first pass.

    Returns:
        The continued run, with the next continuation index.

    Raises:
        FileNotFoundError: The run has no checkpoint or an added file is
            missing.
        ValueError: The run never finished, or an added file's name is
            already in the run.
    """
    saved = json.loads((run_dir / 'checkpoint.json').read_text('utf-8'))
    if saved.get('report') is None:
        raise ValueError(f'{run_dir} has not finished; nothing to continue')
    index = int(saved['continuation']) + 1
    run_id = str(saved['run_id'])
    settings = agent_config or config.AgentConfig.from_dict(saved['config'])
    earlier = state.Task.from_dict(saved['task'])
    folder = run_dir / 'inputs' / f'c{index}'
    names = _combine(earlier.folder, added, folder)
    task = dataclasses.replace(earlier, folder=folder)
    active_memory = graph.resolve_memory_store(
        settings,
        memory_store=memory_store,
        memory_path=memory_path,
        runs_dir=run_dir.parent,
    )
    ingested = ingest.ingest_folder(
        folder, workdir=run_dir / f'db-c{index}', config=ingest_config
    )
    try:
        store = results.ResultStore.from_list(saved.get('results'))
        book = (
            None
            if not settings.ledger
            else ledger.HypothesisLedger.from_dict(saved['ledger'], store)
            if saved.get('ledger')
            else ledger.HypothesisLedger(run_id, store)
        )
        # A check left inconclusive may be settled by the added data.
        reverify = [] if book is None else book.reopen_verification()
        record = trajectory.Trajectory(
            run_dir / 'trajectory.jsonl', run_id, index
        )
        session = graph.build_session(
            task,
            client,
            settings,
            ingested,
            contracts.Run(run_id),
            record,
            store=store,
            book=book,
            continuation=index,
            clock=clock,
            memory_store=active_memory,
        )
        summary = graph.catalog_summary(ingested.catalog)
        changes = what_changed(saved.get('catalog') or {}, summary)
        opening = prompts.WHAT_CHANGED_PROMPT.format(changes='\n'.join(changes))
        if book is not None:
            opening += '\n\nLedger so far:\n' + json.dumps(
                [entry.summary() for entry in book.ranked()], indent=1
            )
        record.record(
            'continuation',
            added=names,
            changes=changes,
            reverify=reverify,
            config=settings.to_dict(),
        )
        run = run_state.RunState(
            session,
            context.Conversation(settings.context, store),
            prompts.has_dependency_data(ingested.catalog),
            run_dir,
            summary,
            continuation=index,
            opening=opening,
        )
        graph.run_graph(run, clock=clock)
    finally:
        ingested.close()
    if run.final is None:
        raise RuntimeError('The graph ended without reaching report')
    if (
        active_memory is not None
        and not active_memory.frozen
        and run.final.causes
    ):
        updated = memory_record.build_record_from_run(
            task, summary, run.final, store
        )
        existing = active_memory.get(run_id)
        if existing is not None:
            updated = updated.carrying_feedback_from(existing)
        active_memory.save(updated)
    return graph.RunResult(run_id, run_dir, run.final, tuple(record.events))
