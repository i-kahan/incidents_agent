"""Re-render a report from a saved trajectory, without calling a model.

Citations in the report are checked again against the full tool results
kept in the trajectory, and the count is printed to stderr.
"""

import argparse
import sys
from pathlib import Path

from incidents_agent.agent import ledger
from incidents_agent.agent import render
from incidents_agent.agent import report
from incidents_agent.agent import results
from incidents_agent.agent import trajectory


def replay(path: Path, continuation: int | None = None) -> tuple[str, int, int]:
    """Render the report recorded in a trajectory.

    Args:
        path: A `trajectory.jsonl`.
        continuation: Which pass to render; the latest when None.

    Returns:
        The Markdown, the number of valid citations, and the total cited.

    Raises:
        ValueError: The trajectory holds no final report for that pass.
    """
    events = trajectory.read_trajectory(path)
    finals = [
        event
        for event in events
        if event['type'] == 'final_report'
        and (continuation is None or event['continuation'] == continuation)
    ]
    if not finals:
        raise ValueError(f'{path} has no final report for that pass')
    body = finals[-1]['report']
    if not isinstance(body, dict):
        raise ValueError(f'{path} has a malformed final report')
    final = report.Report.from_dict(body)
    store = results.ResultStore()
    for event in events:
        if event['type'] == 'tool_call' and event['call_id'] not in store:
            store.add(results.StoredResult.from_dict(event))
    cited = [item for cause in final.causes for item in cause.evidence]
    valid = sum(
        ledger.check_citation(
            item.to_dict(), store, final.run_id, default_stance=item.stance
        )[0]
        is not None
        for item in cited
    )
    return render.render(final, render.call_tools(events)), valid, len(cited)


def main() -> int:
    """Print or write the re-rendered report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trajectory')
    parser.add_argument('--continuation', type=int)
    parser.add_argument('--out')
    args = parser.parse_args()
    try:
        markdown, valid, total = replay(
            Path(args.trajectory), args.continuation
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f'Citations valid: {valid} of {total}', file=sys.stderr)
    if args.out:
        Path(args.out).write_text(markdown, encoding='utf-8')
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
