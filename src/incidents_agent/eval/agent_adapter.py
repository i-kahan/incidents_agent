"""Consume Phase 5 reports and trajectories without changing their contracts."""

import dataclasses
import json
import re
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from incidents_agent.agent import config
from incidents_agent.agent import graph
from incidents_agent.agent import ledger
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.eval import field
from incidents_agent.eval import retry
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger as spend_ledger

ClientFactory = Callable[[state.Task], contracts.ModelClient]


def task_for(case: runner.Case) -> state.Task:
    """Construct a blind task; field onset and gold remain evaluator-only."""
    if case.suite == 'field':
        return state.Task(case.folder, case_id=case.case_id)
    task = state.Task.from_task_json(case.folder / 'task.json')
    if task.case_id != case.case_id:
        raise ValueError('Task case ID differs from selected case')
    return task


def _evidence_items(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in (
                'evidence',
                'evidence_for',
                'evidence_against',
            ) and isinstance(child, list):
                for item in child:
                    yield item if isinstance(item, dict) else {'invalid': item}
            else:
                yield from _evidence_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _evidence_items(child)


def trajectory_metrics(
    events: Sequence[Mapping[str, object]],
    final: report.Report,
) -> dict[str, object]:
    """Recheck attempted and final citations using the upstream validator."""
    store = results.ResultStore()
    attempted = valid = input_tokens = output_tokens = 0
    for event in events:
        if event.get('v') != trajectory.TRAJECTORY_VERSION:
            raise ValueError('Unsupported trajectory version')
        if event.get('run_id') != final.run_id:
            raise ValueError('Trajectory and report run IDs differ')
        if event.get('type') == 'tool_call':
            store.add(results.StoredResult.from_dict(event))
        if event.get('type') == 'model_call':
            usage = event.get('usage')
            if isinstance(usage, dict):
                input_tokens += int(usage.get('input_tokens', 0))
                output_tokens += int(usage.get('output_tokens', 0))
            message = event.get('message')
            calls = (
                message.get('tool_calls', [])
                if isinstance(message, dict)
                else []
            )
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                try:
                    arguments = json.loads(str(call.get('arguments', '{}')))
                except json.JSONDecodeError:
                    continue
                for item in _evidence_items(arguments):
                    attempted += 1
                    checked, _ = ledger.check_citation(
                        item, store, final.run_id
                    )
                    valid += checked is not None
    final_items = [item for cause in final.causes for item in cause.evidence]
    final_valid = sum(
        ledger.check_citation(item.to_dict(), store, final.run_id)[0]
        is not None
        for item in final_items
    )
    return {
        'citation_attempts': attempted,
        'valid_citation_attempts': valid,
        'citation_validity': valid / attempted if attempted else None,
        'final_citations': len(final_items),
        'valid_final_citations': final_valid,
        'outcome': final.outcome,
        'stop_reason': final.stop_reason,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'tool_calls': final.counters.get('tool_calls', 0),
        'model_turns': sum(e.get('type') == 'model_call' for e in events),
        'cost_usd': float(str(final.counters.get('cost_usd', 0))),
        'wall_time_s': float(str(final.counters.get('elapsed_s', 0))),
    }


def _document_rows(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, dict):
        if isinstance(value.get('chunk_id'), str):
            yield value
        else:
            for child in value.values():
                yield from _document_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from _document_rows(child)


def field_prediction(
    final: report.Report,
    events: Sequence[Mapping[str, object]],
    catalog: Mapping[str, object],
) -> field.Prediction:
    """Bind field evidence to its actual source and validated final citation."""
    store = results.ResultStore()
    observations = []
    raw_tables = catalog.get('tables', {})
    if not isinstance(raw_tables, dict):
        raise ValueError('Catalog tables must be a mapping')
    table_sources = {
        str(name): str(info['source_file'])
        for name, info in raw_tables.items()
        if isinstance(info, dict) and 'source_file' in info
    }
    supported_components = [
        cause.component
        for cause in final.causes
        if cause.cited or not final.ledger_enabled
    ]
    for event in events:
        if event.get('type') == 'ledger':
            book = event.get('ledger')
            if isinstance(book, dict):
                supported_components.extend(
                    str(entry['component'])
                    for entry in _dicts(book.get('entries'))
                    if entry.get('status') == 'supported'
                )
        if event.get('type') != 'tool_call':
            continue
        item = results.StoredResult.from_dict(event)
        store.add(item)
        if not item.ok or item.from_memory:
            continue
        payload = results.payload(item.result)
        if item.tool in ('read_document', 'search_documents'):
            for row in _document_rows(payload):
                source = str(row['chunk_id']).split('::', 1)[0]
                observations.append(
                    field.Observation(
                        item.call_id,
                        source,
                        json.dumps(row, ensure_ascii=False),
                    )
                )
            continue
        table = (item.locator or {}).get('table')
        if table not in table_sources and item.tool == 'run_sql':
            query = str(item.args.get('query', ''))
            names = [
                name
                for name in table_sources
                if re.search(r'\b' + re.escape(name) + r'\b', query)
            ]
            table = names[0] if len(names) == 1 else None
        if isinstance(table, str) and table in table_sources:
            observations.append(
                field.Observation(
                    item.call_id,
                    table_sources[table],
                    json.dumps(payload, ensure_ascii=False),
                )
            )
    citations = []
    for cause in final.causes:
        for reference in cause.evidence:
            checked, _ = ledger.check_citation(
                reference.to_dict(), store, final.run_id
            )
            if checked is None or checked.stance != 'for':
                continue
            stored = store.get(checked.call_id)
            if stored is None:
                continue
            if stored.tool in ('read_document', 'search_documents'):
                source = checked.locator.split('::', 1)[0]
                if '::' not in checked.locator:
                    continue
            else:
                sources = {
                    obs.source_file
                    for obs in observations
                    if obs.call_id == checked.call_id
                }
                if len(sources) != 1:
                    continue
                source = next(iter(sources))
            citations.append(
                field.Citation(checked.call_id, checked.quote, source)
            )
    by_hypothesis = {
        cause.hypothesis_id: cause.component for cause in final.causes
    }
    action_components = tuple(
        by_hypothesis.get(action.hypothesis_id, action.hypothesis_id)
        for action in final.actions
        if action.risk != 'read_only'
    ) + tuple(
        action.action for action in final.actions if action.risk != 'read_only'
    )
    return field.Prediction(
        final.ranks,
        final.fault_type,
        final.outcome,
        tuple(observations),
        tuple(citations),
        tuple(supported_components),
        action_components,
    )


def inspection_events(
    events: Sequence[Mapping[str, object]],
    candidates: Sequence[str],
) -> list[Mapping[str, object]]:
    """Project candidate names found in returned SQL rows or document text."""
    patterns = {
        name: re.compile(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', re.I)
        for name in candidates
        if name
    }
    projected = []
    for event in events:
        if event.get('type') != 'tool_call' or event.get('ok') is not True:
            projected.append(event)
            continue
        item = results.StoredResult.from_dict(event)
        payload = results.payload(item.result)
        if item.tool in ('search_documents', 'read_document'):
            evidence = ' '.join(
                str(row.get('text', row.get('excerpt', '')))
                for row in _document_rows(payload)
            )
        elif item.tool == 'run_sql':
            evidence = json.dumps(
                payload.get('content', {}), ensure_ascii=False
            )
        else:
            projected.append(event)
            continue
        hits = {
            name
            for name, pattern in patterns.items()
            if pattern.search(evidence)
        }
        projected.append(
            {
                **event,
                'target_components': sorted(hits | set(item.target_components)),
            }
        )
    return projected


class _CallCounter:
    """Delegating client that records whether any model call was made."""

    def __init__(self, inner: contracts.ModelClient | None) -> None:
        """Wrap `inner`; `calls` counts requests, including failed ones."""
        self._inner = inner
        self.calls = 0

    def complete(
        self,
        request: contracts.ChatRequest,
        *,
        run: contracts.Run,
        case_id: str | None = None,
    ) -> contracts.ChatResult:
        """Count the request, then delegate it."""
        if self._inner is None:
            raise RuntimeError('No model client was configured')
        self.calls += 1
        return self._inner.complete(request, run=run, case_id=case_id)


class Executor:
    """Adapt the owned Phase 5 run function to the evaluation cache."""

    def __init__(
        self,
        client_factory: ClientFactory,
        settings: config.AgentConfig,
        *,
        final: bool = False,
        spend: spend_ledger.SpendLedger | None = None,
    ) -> None:
        """Use a client factory and the existing shared spend ledger."""
        self.client_factory = client_factory
        self.settings = settings
        self.reserve = 'final' if final else None
        self.spend = spend

    def __call__(
        self, case: runner.Case, attempts: Path, run_id: str
    ) -> dict[str, object]:
        """Run once or recover an already written report without model calls."""
        try:
            task = task_for(case)
        except (ValueError, FileNotFoundError) as task_error:
            raise retry.AttemptFailure(
                retry.classify(task_error)
            ) from task_error
        folder = attempts / run_id
        started = time.monotonic()
        recovering = (folder / 'report.json').exists()
        counter = _CallCounter(None)
        try:
            if not recovering:
                counter = _CallCounter(self.client_factory(task))
                graph.run_investigation(
                    task,
                    counter,
                    self.settings,
                    runs_dir=attempts,
                    run_id=run_id,
                    reserve_name=self.reserve,
                )
        except (ValueError, FileNotFoundError) as input_error:
            if counter.calls:
                # After a model call this is a defect, not a bad input: let
                # it abort the run instead of caching a free-looking miss.
                raise
            raise retry.AttemptFailure(
                retry.classify(input_error)
            ) from input_error
        finally:
            if self.spend is not None and not recovering:
                counters = self.spend.counters(
                    contracts.Run(run_id, self.reserve)
                )
                usage = {
                    **dataclasses.asdict(counters.usage),
                    'cost_usd': float(counters.spent),
                    'held_usd': float(counters.held),
                    'wall_time_s': time.monotonic() - started,
                }
                runner.atomic_json(folder / 'usage.json', usage)
        final = report.Report.from_dict(
            runner.read_object(folder / 'report.json')
        )
        events = trajectory.read_trajectory(folder / 'trajectory.jsonl')
        metrics = trajectory_metrics(events, final)
        if (folder / 'usage.json').exists():
            metrics.update(runner.read_object(folder / 'usage.json'))
        elif self.spend is not None:
            # A crash can leave a durable report before the usage write.
            counters = self.spend.counters(contracts.Run(run_id, self.reserve))
            metrics.update(dataclasses.asdict(counters.usage))
            metrics.update(
                cost_usd=float(counters.spent), held_usd=float(counters.held)
            )
        runner.atomic_json(folder / 'usage.json', metrics)
        projected = folder / 'rcaeval-trajectory.jsonl'
        scoring.export_trajectory(
            inspection_events(events, task.candidate_services), projected
        )
        checkpoint = runner.read_object(folder / 'checkpoint.json')
        catalog = checkpoint.get('catalog', {})
        if not isinstance(catalog, dict):
            raise ValueError('Run checkpoint lacks its source catalog')
        view = (
            field_prediction(final, events, catalog)
            if case.suite == 'field'
            else None
        )
        output: dict[str, object] = {
            'ranks': list(final.ranks),
            'model': next(
                (
                    event.get('requested_model')
                    for event in events
                    if event.get('type') == 'model_call'
                ),
                self.settings.model_alias,
            ),
            'usage': metrics,
            'trajectory': str(projected.resolve()),
            'canonical_trajectory': str(
                (folder / 'trajectory.jsonl').resolve()
            ),
            'report': final.to_dict(),
            'primary_model': self.settings.model_alias,
            'requested_models': sorted(
                {
                    str(e['requested_model'])
                    for e in events
                    if e.get('type') == 'model_call'
                }
            ),
            'served_models': sorted(
                {
                    str(e['served_model'])
                    for e in events
                    if e.get('type') == 'model_call'
                }
            ),
            'forced': checkpoint.get('submission') is None
            or any(
                reason.startswith('forced finalisation:')
                for cause in final.causes
                for reason in cause.confidence_reasons
            ),
        }
        errors = [
            event for event in events if event.get('type') == 'model_error'
        ]
        if errors:
            error = str(errors[-1].get('error'))
            output.update(ranks=[], error=error, stop_reason=final.stop_reason)
            kinds: dict[str, Exception] = {
                'ProviderError': contracts.ProviderError(error),
                'RetryDeferred': contracts.RetryDeferred(
                    float(str(errors[-1].get('retry_after') or 0))
                ),
                'ModelUnavailable': contracts.ModelUnavailable(error),
                'BudgetRefused': contracts.BudgetRefused(error),
                'BillingUncertain': contracts.BillingUncertain(error),
            }
            if error not in kinds:
                raise ValueError(f'Unknown model failure: {error}')
            raise retry.AttemptFailure(
                retry.classify(
                    kinds[error],
                    held=bool(metrics.get('held_usd')),
                )
            )
        if view is not None and not errors:
            output['field_prediction'] = dataclasses.asdict(view)
        return output


def restore_prediction(data: Mapping[str, object]) -> field.Prediction:
    """Restore the evaluator view stored in the cache; no gold is involved."""
    return field.Prediction(
        tuple(str(v) for v in data['ranks'])
        if isinstance(data['ranks'], list)
        else (),
        str(data['fault_category'])
        if data.get('fault_category') is not None
        else None,
        str(data['outcome']),
        tuple(
            field.Observation(
                str(x['call_id']), str(x['source_file']), str(x['content'])
            )
            for x in _dicts(data.get('observations'))
        ),
        tuple(
            field.Citation(
                str(x['call_id']), str(x['quote']), str(x['source_file'])
            )
            for x in _dicts(data.get('citations'))
        ),
        tuple(str(v) for v in _list(data.get('supported_components'))),
        tuple(str(v) for v in _list(data.get('action_components'))),
    )


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _dicts(value: object) -> list[dict[str, object]]:
    return [item for item in _list(value) if isinstance(item, dict)]
