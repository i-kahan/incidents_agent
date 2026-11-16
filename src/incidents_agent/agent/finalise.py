"""The one finalisation path every run ends through, however it stops.

A normal submit, any limit, a refused reservation, a model failure and the
plain loop all come here. The ranking is rendered deterministically from
the ledger (or, with the ledger off, from the model's last stated ranking),
every ranked claim is checked against valid evidence, and the result is
labelled verified, provisional, inconclusive or no fault found.
"""

import dataclasses
from collections.abc import Mapping

from incidents_agent.agent import investigate
from incidents_agent.agent import ledger
from incidents_agent.agent import prompts
from incidents_agent.agent import report

_MAX_DEFAULT_REQUESTS = 3


@dataclasses.dataclass(frozen=True)
class Verdict:
    """A verification outcome for one component when the ledger is off."""

    outcome: report.Verification
    check: str


def _cap(
    level: report.Confidence, ceiling: report.Confidence
) -> report.Confidence:
    order = report.CONFIDENCE_LEVELS
    return order[min(order.index(level), order.index(ceiling))]


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def finalise(
    session: investigate.Session,
    *,
    submission: Mapping[str, object] | None,
    stop_reason: str,
    verdicts: Mapping[str, Verdict] | None = None,
    continuation: int = 0,
) -> report.Report:
    """Render the final report for a run.

    Args:
        session: The run.
        submission: Validated `submit` arguments, or None when the run was
            forced to end without one.
        stop_reason: Why the run ended.
        verdicts: Verification outcomes by component, ledger off only.
        continuation: Continuation index.

    Returns:
        The validated report.
    """
    dropped: list[str] = []
    forced = submission is None
    if submission is not None and submission.get('outcome') == 'no_fault_found':
        return _report(
            session,
            'no_fault_found',
            stop_reason,
            submission,
            (),
            (),
            (),
            dropped,
            continuation,
        )
    if session.ledger is not None:
        causes, forced = _ledger_causes(
            session, session.ledger, submission, stop_reason, dropped
        )
    else:
        causes = _plain_causes(
            session, submission, stop_reason, verdicts or {}, dropped
        )
    top = causes[0] if causes else None
    if top is None:
        outcome: report.Outcome = 'inconclusive'
    elif top.verification == 'supported' and not forced:
        outcome = 'verified'
    else:
        outcome = 'provisional'
    actions = _actions(submission, causes, dropped)
    requests = (
        _data_requests(session, submission, causes, outcome, dropped)
        if outcome in ('provisional', 'inconclusive')
        else ()
    )
    return _report(
        session,
        outcome,
        stop_reason,
        submission,
        causes,
        actions,
        requests,
        dropped,
        continuation,
    )


def _ledger_causes(
    session: investigate.Session,
    book: ledger.HypothesisLedger,
    submission: Mapping[str, object] | None,
    stop_reason: str,
    dropped: list[str],
) -> tuple[tuple[report.RankedCause, ...], bool]:
    entries: list[ledger.Hypothesis] = []
    for name in _strings(None if submission is None else submission['ranks']):
        entry = book.find(name)
        if entry is None:
            dropped.append(f'rank {name!r}: no open or supported hypothesis')
        elif not book.has_data_support(entry):
            dropped.append(f'rank {name!r}: no valid supporting citation')
        else:
            entries.append(entry)
    forced = not entries
    if forced:
        entries = [
            entry
            for entry in book.ranked()
            if entry.status != 'rejected' and book.has_data_support(entry)
        ]
    entries = _within_candidates(session, entries, dropped)
    causes = []
    for rank, entry in enumerate(entries, 1):
        reasons = [
            f'{len(entry.evidence_for)} valid supporting citation(s)',
            f'{len(entry.evidence_against)} contrary citation(s)',
            f'verification: {entry.verification}'
            + (
                f' ({entry.verification_check})'
                if entry.verification_check
                else ''
            ),
        ]
        level = entry.confidence
        if rank == 1 and submission is not None and not forced:
            reasons.extend(_strings(submission.get('confidence_reasons')))
        if entry.verification != 'supported':
            level = _cap(level, 'medium')
        if forced:
            level = _cap(level, 'low')
            reasons.append(f'forced finalisation: {stop_reason}')
        causes.append(
            report.RankedCause(
                rank=rank,
                component=entry.component,
                statement=entry.statement,
                hypothesis_id=entry.id,
                verification=entry.verification,
                confidence=level,
                confidence_reasons=tuple(reasons),
                evidence=(*entry.evidence_for, *entry.evidence_against),
            )
        )
    return tuple(causes), forced


def _within_candidates(
    session: investigate.Session,
    entries: list[ledger.Hypothesis],
    dropped: list[str],
) -> list[ledger.Hypothesis]:
    task = session.task
    if not task.has_candidates:
        return entries
    allowed = {
        name.casefold()
        for name in (*task.candidate_services, *task.candidate_indicators)
    }
    kept = []
    for entry in entries:
        if entry.component.casefold() in allowed:
            kept.append(entry)
        else:
            dropped.append(f'rank {entry.component!r}: not a candidate')
    return kept


def _plain_causes(
    session: investigate.Session,
    submission: Mapping[str, object] | None,
    stop_reason: str,
    verdicts: Mapping[str, Verdict],
    dropped: list[str],
) -> tuple[report.RankedCause, ...]:
    forced = submission is None
    names = _strings(
        list(session.stated_ranks)
        if submission is None
        else submission['ranks']
    )
    task = session.task
    allowed = {
        name.casefold(): name
        for name in (*task.candidate_services, *task.candidate_indicators)
    }
    if allowed:
        for name in names:
            if name.casefold() not in allowed:
                dropped.append(f'rank {name!r}: not a candidate')
        names = tuple(
            allowed[name.casefold()]
            for name in names
            if name.casefold() in allowed
        )
    stated = None if submission is None else submission.get('confidence')
    causes = []
    for rank, name in enumerate(dict.fromkeys(names), 1):
        verdict = verdicts.get(name.casefold())
        verification = 'unverified' if verdict is None else verdict.outcome
        level: report.Confidence = (
            report.pick(stated, report.CONFIDENCE_LEVELS) or 'low'
            if rank == 1
            else 'low'
        )
        reasons = ['ledger off: no citations are checked']
        if verdict is not None:
            reasons.append(f'verification: {verdict.outcome} ({verdict.check})')
        if verification != 'supported':
            level = _cap(level, 'medium')
        if forced:
            level = _cap(level, 'low')
            reasons.append(f'forced finalisation: {stop_reason}')
        causes.append(
            report.RankedCause(
                rank=rank,
                component=name,
                statement=f'{name} is the root cause',
                hypothesis_id=None,
                verification=verification,
                confidence=level,
                confidence_reasons=tuple(reasons),
                evidence=(),
            )
        )
    return tuple(causes)


def _actions(
    submission: Mapping[str, object] | None,
    causes: tuple[report.RankedCause, ...],
    dropped: list[str],
) -> tuple[report.Action, ...]:
    if submission is None:
        return ()
    targets = {cause.hypothesis_id or cause.component for cause in causes}
    actions: list[report.Action] = []
    raw = submission.get('actions')
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            dropped.append(f'action {index}: not an object')
            continue
        target = str(item.get('hypothesis_id', ''))
        risk = item.get('risk')
        text = str(item.get('action', '')).strip()
        check = str(item.get('how_to_verify', '')).strip()
        if target not in targets:
            dropped.append(
                f'action {index}: {target!r} is not a ranked hypothesis'
            )
        elif (level := report.pick(risk, report.RISK_ORDER)) is None:
            dropped.append(f'action {index}: unknown risk {risk!r}')
        elif not text or not check:
            dropped.append(f'action {index}: needs an action and a check')
        else:
            actions.append(report.Action(target, text, level, check))
    return tuple(
        sorted(actions, key=lambda action: report.RISK_ORDER.index(action.risk))
    )


def _data_requests(
    session: investigate.Session,
    submission: Mapping[str, object] | None,
    causes: tuple[report.RankedCause, ...],
    outcome: report.Outcome,
    dropped: list[str],
) -> tuple[report.DataRequest, ...]:
    book = session.ledger
    if book is None:
        return ()
    undecided = {
        entry.id: entry
        for entry in book.ranked()
        if entry.status != 'rejected'
        and (
            entry.status == 'open'
            or entry.verification in ('inconclusive', 'unverified')
        )
    }
    requests: list[report.DataRequest] = []
    raw = None if submission is None else submission.get('data_requests')
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            dropped.append(f'data request {index}: not an object')
            continue
        target = str(item.get('hypothesis_id', ''))
        fields = [
            str(item.get(name, '')).strip()
            for name in ('what', 'source', 'effect')
        ]
        if target not in undecided:
            dropped.append(
                f'data request {index}: {target!r} is not an open or '
                'undecided hypothesis'
            )
        elif not all(fields):
            dropped.append(f'data request {index}: needs what, source, effect')
        else:
            requests.append(report.DataRequest(target, *fields))
    covered = {request.hypothesis_id for request in requests}
    ranked_ids = [cause.hypothesis_id for cause in causes]
    order = [hid for hid in ranked_ids if hid in undecided] or (
        list(undecided) if outcome == 'inconclusive' else []
    )
    for hypothesis_id in order[:_MAX_DEFAULT_REQUESTS]:
        if hypothesis_id is None or hypothesis_id in covered:
            continue
        component = undecided[hypothesis_id].component
        requests.append(
            report.DataRequest(
                hypothesis_id,
                f'Metrics, logs and calls to and from {component} around '
                'the fault start, at the finest resolution available',
                f'the monitoring and logging system that covers {component}',
                f'Confirms {hypothesis_id} if {component} degrades before '
                'the components that call it; refutes it if it degrades '
                'only after them.',
            )
        )
    return tuple(requests)


def _report(
    session: investigate.Session,
    outcome: report.Outcome,
    stop_reason: str,
    submission: Mapping[str, object] | None,
    causes: tuple[report.RankedCause, ...],
    actions: tuple[report.Action, ...],
    requests: tuple[report.DataRequest, ...],
    dropped: list[str],
    continuation: int,
) -> report.Report:
    fault = None if submission is None else submission.get('fault_type')
    if fault is None and causes and session.ledger is not None:
        entry = session.ledger.entries.get(str(causes[0].hypothesis_id))
        fault = None if entry is None else entry.fault_type
    return report.Report(
        run_id=session.run_id,
        continuation=continuation,
        outcome=outcome,
        stop_reason=stop_reason,
        fault_type=None if fault is None else str(fault),
        ranks=tuple(cause.component for cause in causes),
        causes=causes,
        actions=actions,
        data_requests=requests,
        dropped=tuple(dropped),
        counters={
            **session.budget.to_dict(),
            'stored_calls': len(session.executor.store),
        },
        prompt_hash=prompts.prompt_hash(),
        ledger_enabled=session.ledger is not None,
        verify_enabled=session.config.verify,
    )
