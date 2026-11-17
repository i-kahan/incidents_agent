"""The run state every graph node acts on, and its checkpoint.

Shared by the single-agent graph (`graph`) and the multi-agent graph
(Phase 07), so neither imports the other's wiring.
"""

import dataclasses
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from pathlib import Path

from incidents_agent.agent import context
from incidents_agent.agent import finalise
from incidents_agent.agent import investigate
from incidents_agent.agent import render
from incidents_agent.agent import report
from incidents_agent.agent import state

CHECKPOINT_VERSION = 1
START_MESSAGE = (
    'Investigate the incident described in the briefing and find its root '
    'cause.'
)


@dataclasses.dataclass
class RunState:
    """Everything one run carries between nodes and into its checkpoint.

    Attributes:
        session: Shared run state for the loop.
        conversation: Message history.
        catalog_has_dependencies: Whether dependency data was detected.
        catalog: Summary of the ingested catalog, for continuations.
        run_dir: Folder for the run record, or None to keep it in memory.
        continuation: Continuation index.
        opening: First user message of this pass.
        submission: Last accepted `submit` arguments.
        stop_reason: Why investigation (or the run) stopped.
        verify_runs: How many times verify has run.
        corrected: Whether the single return to investigate was used.
        pending_correction: Refutation that sends the run back, if any.
        verdicts: Verification outcomes by component, ledger off.
        final: The finished report.
        extra: Mode-specific checkpoint fields (multi mode: budget, briefs,
            verdicts), written over the common ones.
    """

    session: investigate.Session
    conversation: context.Conversation
    catalog_has_dependencies: bool
    run_dir: Path | None
    catalog: dict[str, object] = dataclasses.field(default_factory=dict)
    continuation: int = 0
    opening: str = START_MESSAGE
    submission: dict[str, object] | None = None
    stop_reason: str = 'submitted'
    verify_runs: int = 0
    corrected: bool = False
    pending_correction: dict[str, str] | None = None
    verdicts: dict[str, finalise.Verdict] = dataclasses.field(
        default_factory=dict
    )
    final: report.Report | None = None
    extra: dict[str, object] = dataclasses.field(default_factory=dict)

    @property
    def budget(self) -> state.Budget:
        """Return the single-agent budget, with its verification reserve.

        Raises:
            TypeError: The session draws on a role allowance instead.
        """
        budget = self.session.budget
        if not isinstance(budget, state.Budget):
            raise TypeError('This run has no single-agent budget')
        return budget

    def checkpoint(self, node: str) -> None:
        """Write the state after `node` to `checkpoint.json`, atomically."""
        if self.run_dir is None:
            return
        session = self.session
        data = {
            'v': CHECKPOINT_VERSION,
            'run_id': session.run_id,
            'continuation': self.continuation,
            'node': node,
            'written_at': datetime.now(UTC).isoformat(),
            'task': session.task.to_dict(),
            'config': session.config.to_dict(),
            'briefing': session.briefing,
            'catalog': self.catalog,
            'budget': session.budget.to_dict(),
            'ledger': (
                None if session.ledger is None else session.ledger.to_dict()
            ),
            'results': session.executor.store.to_list(),
            'conversation': self.conversation.to_dict(),
            'stated_ranks': list(session.stated_ranks),
            'submission': self.submission,
            'stop_reason': self.stop_reason,
            'verify_runs': self.verify_runs,
            'corrected': self.corrected,
            'verdicts': {
                key: dataclasses.asdict(value)
                for key, value in self.verdicts.items()
            },
            'report': None if self.final is None else self.final.to_dict(),
            **self.extra,
        }
        write_json(self.run_dir / 'checkpoint.json', data)


def finish(
    run: RunState, *, counters: Mapping[str, object] | None = None
) -> None:
    """Finalise the run and write `report.json` and `report.md`.

    Args:
        run: The run; `final` is set on return.
        counters: Run totals for the report; None uses the session budget.
    """
    session = run.session
    run.final = finalise.finalise(
        session,
        submission=run.submission,
        stop_reason=run.stop_reason,
        verdicts=run.verdicts,
        continuation=run.continuation,
        counters=counters,
    )
    session.record.record('final_report', report=run.final.to_dict())
    if run.run_dir is None:
        return
    markdown = render.render(
        run.final, render.call_tools(session.record.events)
    )
    suffixes = [''] + ([f'-c{run.continuation}'] if run.continuation else [])
    for suffix in suffixes:
        write_json(run.run_dir / f'report{suffix}.json', run.final.to_dict())
        (run.run_dir / f'report{suffix}.md').write_text(
            markdown, encoding='utf-8'
        )


def write_json(path: Path, data: object) -> None:
    """Write JSON to `path` through a temporary file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    with os.fdopen(handle, 'w', encoding='utf-8') as output:
        json.dump(data, output, indent=2, ensure_ascii=False, default=str)
        output.write('\n')
    os.replace(temp, path)


def target(run: RunState) -> tuple[str, str] | None:
    """Return the hypothesis to challenge: (id, component), or None.

    The submitted ranking is tried first, then the ledger order. A target
    must not be rejected and must have support from this run's data. With
    the ledger off, the top stated rank is used.

    Args:
        run: The run.

    Returns:
        The target's hypothesis id and component, or None.
    """
    session = run.session
    ranks = None if run.submission is None else run.submission.get('ranks')
    names = (
        tuple(str(name) for name in ranks)
        if isinstance(ranks, list)
        else session.stated_ranks
    )
    book = session.ledger
    if book is None:
        return (names[0], names[0]) if names else None
    candidates = [book.find(name) for name in names]
    candidates += book.ranked()
    for entry in candidates:
        if (
            entry is not None
            and entry.status != 'rejected'
            and book.has_data_support(entry)
        ):
            return entry.id, entry.component
    return None
