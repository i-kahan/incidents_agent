"""Turn a finished agent run into an incident-memory record.

This lives in `agent/`, not `memory/`: it reads the agent's report and result
types, and the memory package must not depend on them (the agent depends on
tools, which depend on memory).
"""

from collections.abc import Mapping

from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import state
from incidents_agent.memory import store as incident_memory


def build_record_from_run(
    task: state.Task,
    catalog: Mapping[str, object],
    final: report.Report,
    store: results.ResultStore | None = None,
) -> incident_memory.MemoryRecord:
    """Build an `unverified` memory record from a finished run.

    Args:
        task: Investigated task.
        catalog: Catalog summary dictionary from the run checkpoint.
        final: Final report produced by the agent.
        store: Optional tool result store for extra component context.

    Returns:
        A `MemoryRecord` marked `unverified`.
    """
    summary_parts: list[str] = []
    if task.question.strip():
        summary_parts.append(task.question.strip())
    if final.fault_type:
        summary_parts.append(f'fault_type: {final.fault_type}')
    for cause in final.causes:
        if cause.statement.strip():
            summary_parts.append(cause.statement.strip())

    key_evidence: list[str] = []
    for cause in final.causes:
        for ref in cause.evidence:
            if ref.stance == 'for':
                entry = f'{ref.locator}: {ref.quote}'
                if entry not in key_evidence:
                    key_evidence.append(entry)
    summary_parts.extend(key_evidence)

    raw_tables = catalog.get('tables')
    if isinstance(raw_tables, Mapping) and raw_tables:
        names = ', '.join(sorted(str(name) for name in raw_tables))
        summary_parts.append(f'tables: {names}')
    raw_docs = catalog.get('documents')
    if isinstance(raw_docs, list) and raw_docs:
        docs = ', '.join(str(doc) for doc in raw_docs)
        summary_parts.append(f'documents: {docs}')

    symptom_summary = ' | '.join(summary_parts) or f'outcome={final.outcome}'

    components: list[str] = []
    seen_components: set[str] = set()

    def _add_component(name: str) -> None:
        cleaned = name.strip()
        key = incident_memory.normalise_component(cleaned)
        if cleaned and key not in seen_components:
            seen_components.add(key)
            components.append(cleaned)

    for rank in final.ranks:
        _add_component(rank)
    for cause in final.causes:
        _add_component(cause.component)
    if store is not None:
        for item in store:
            if item.ok and not item.from_memory:
                for comp in item.target_components:
                    _add_component(comp)

    diagnosed_root_cause = (
        final.ranks[0]
        if final.outcome != 'no_fault_found' and final.ranks
        else None
    )
    recommended_actions = tuple(
        action.action for action in final.actions if action.action.strip()
    )
    return incident_memory.MemoryRecord(
        run_id=final.run_id,
        symptom_summary=symptom_summary,
        affected_components=tuple(components),
        diagnosed_root_cause=diagnosed_root_cause,
        fault_type=final.fault_type,
        key_evidence=tuple(key_evidence),
        recommended_actions=recommended_actions,
        outcome='unverified',
        what_fixed_it=None,
    )
