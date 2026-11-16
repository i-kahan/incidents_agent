"""Render a report as cited Markdown, deterministically."""

from collections.abc import Iterable
from collections.abc import Mapping

from incidents_agent.agent import report

_OUTCOME_TEXT = {
    'verified': 'Verified: the top cause survived a check that could have '
    'disproved it.',
    'provisional': 'Provisional: a ranking exists but the top cause was not '
    'confirmed by a disproof check.',
    'inconclusive': 'Inconclusive: no cause could be supported by the data.',
    'no_fault_found': 'No fault found: the data was examined and nothing '
    'abnormal was found.',
}
_RISK_TEXT = {
    'read_only': 'read-only check',
    'reversible': 'reversible change',
    'disruptive': 'disruptive change',
}


def call_tools(events: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Map each data-tool call id in a trajectory to its tool name."""
    return {
        str(event['call_id']): str(event['tool'])
        for event in events
        if event.get('type') == 'tool_call'
    }


def _cell(text: object) -> str:
    return str(text).replace('|', '\\|').replace('\n', ' ')


def render(final: report.Report, tools: Mapping[str, str] | None = None) -> str:
    """Return the report as Markdown.

    Args:
        final: The report.
        tools: Tool name per call id, to name the source of each citation.

    Returns:
        Markdown text ending in a newline; identical for identical input.
    """
    names = tools or {}
    title = f'# Incident report — {final.run_id}'
    if final.continuation:
        title += f' (continuation {final.continuation})'
    lines = [
        title,
        '',
        f'**Outcome:** {_OUTCOME_TEXT[final.outcome]}',
        '',
        f'- Stop reason: `{final.stop_reason}`',
        f'- Fault type: {final.fault_type or "not stated"}',
        f'- Ledger: {"on" if final.ledger_enabled else "off"}; '
        f'verify step: {"on" if final.verify_enabled else "off"}',
        '- Confidence is a qualitative level with its reasons; no '
        'probability is shown.',
        '- Recommended actions are suggestions; the agent ran none of them.',
        '',
        '## Ranked causes',
        '',
    ]
    if not final.causes:
        lines += ['No cause is ranked.', '']
    else:
        lines += [
            '| Rank | Component | Verification | Confidence |',
            '|---|---|---|---|',
        ]
        lines += [
            f'| {cause.rank} | {_cell(cause.component)} | '
            f'{cause.verification} | {cause.confidence} |'
            for cause in final.causes
        ]
        lines.append('')
    for cause in final.causes:
        label = f' ({cause.hypothesis_id})' if cause.hypothesis_id else ''
        lines += [
            f'### {cause.rank}. {cause.component}{label}',
            '',
            cause.statement,
            '',
            'Why this confidence:',
            *[f'- {reason}' for reason in cause.confidence_reasons],
            '',
        ]
        if cause.evidence:
            lines.append('Evidence:')
            lines += [
                f'- [{item.stance}] `{item.call_id}`'
                f'{" " + names[item.call_id] if item.call_id in names else ""}'
                f', at `{item.locator}`: "{item.quote}"'
                for item in cause.evidence
            ]
            lines.append('')
    lines += ['## Recommended actions', '']
    if final.actions:
        lines += [
            f'{index}. **{_RISK_TEXT[action.risk]}** for '
            f'{action.hypothesis_id}: {action.action} — how to tell it '
            f'worked: {action.how_to_verify}'
            for index, action in enumerate(final.actions, 1)
        ]
    else:
        lines.append('None.')
    lines.append('')
    if final.data_requests:
        lines += ['## Data that would settle it', '']
        lines += [
            f'- For {item.hypothesis_id}: {item.what} (from {item.source}). '
            f'{item.effect}'
            for item in final.data_requests
        ]
        lines.append('')
    if final.dropped:
        lines += ['## Dropped by validation', '']
        lines += [f'- {item}' for item in final.dropped]
        lines.append('')
    counters = final.counters
    lines += [
        '## Run',
        '',
        f'- Tool calls: {counters.get("tool_calls")}; model turns: '
        f'{counters.get("model_turns")}; model cost: '
        f'${counters.get("cost_usd")}',
        f'- Prompt hash: `{final.prompt_hash}`',
        '',
    ]
    return '\n'.join(lines)
