"""Investigate a folder of incident files and write a cited report.

With --fake a rule-based stand-in model runs the whole agent at no cost.
Without it, the configured model is called through the spend guard, which
requires --yes after the cost notice.
"""

import argparse
import logging
import sys
from pathlib import Path

from incidents_agent.agent import config as agent_config
from incidents_agent.agent import continuation
from incidents_agent.agent import fake_policy
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import litellm_transport
from incidents_agent.llm import pricing
from scripts import budget
from scripts import check_models

RUNS_DIR = Path('runs')
DRY_RUNS_DIR = RUNS_DIR / 'dry'


def _task(args: argparse.Namespace) -> state.Task:
    folder = Path(args.folder)
    task_json = Path(args.task_json) if args.task_json else None
    if task_json is None and (folder / 'task.json').is_file():
        task_json = folder / 'task.json'
    task = (
        state.Task.from_task_json(task_json)
        if task_json is not None
        else state.Task(folder)
    )
    return state.Task(
        folder=folder,
        case_id=task.case_id,
        question=args.question or task.question,
        fault_start_s=(
            args.fault_start
            if args.fault_start is not None
            else task.fault_start_s
        ),
        candidate_services=task.candidate_services,
        candidate_indicators=task.candidate_indicators,
    )


def _paid_client(
    args: argparse.Namespace, settings: agent_config.AgentConfig
) -> contracts.ModelClient:
    env = config.environment(Path('.env'))
    models = config.load_models(check_models.MODELS_FILE)
    alias = settings.model_alias or models.default
    info = client.model_info(models, env, pricing.bundled_catalog(), alias)
    if not info.usable:
        raise config.ConfigError(f'{alias}: {"; ".join(info.issues)}')
    spend = budget.open_ledger(args.budget_usd, env)
    guarded = client.GuardedClient(
        models, env, spend, litellm_transport.LiteLLMTransport()
    )
    snapshot = spend.snapshot()
    print(
        f'This run calls {alias} ({info.spec.id}). Per-case cost cap: '
        f'${settings.limits.cost_usd}; spend guard remaining: '
        f'${snapshot.remaining:f} of ${snapshot.limit:f}.',
        file=sys.stderr,
    )
    if not args.yes:
        raise config.ConfigError('Paid run not confirmed; pass --yes')
    return guarded


def main() -> int:
    """Run or continue an investigation and print where the report is."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', nargs='?', help='folder of incident files')
    parser.add_argument('--task-json', help='RCAEval task.json (blind view)')
    parser.add_argument('--question', default='')
    parser.add_argument('--fault-start', type=float)
    parser.add_argument('--fake', action='store_true')
    parser.add_argument('--yes', action='store_true', help='confirm spend')
    parser.add_argument('--config', default=str(agent_config.DEFAULT_PATH))
    parser.add_argument('--model', help='model alias for this run')
    parser.add_argument('--budget-usd')
    parser.add_argument('--runs-dir')
    parser.add_argument(
        '--memory', action='store_true', help='enable incident memory'
    )
    parser.add_argument('--memory-path', help='SQLite path for incident memory')
    parser.add_argument('--continue-run', metavar='RUN_DIR')
    parser.add_argument('--add', action='append', default=[], metavar='PATH')
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format='%(levelname)s: %(message)s'
    )
    if args.continue_run is None and args.folder is None:
        parser.error('give a folder, or --continue-run with --add')
    if args.continue_run is not None and not args.add:
        parser.error('--continue-run needs at least one --add')
    try:
        settings = agent_config.load_agent_config(Path(args.config))
        overrides: dict[str, object] = {}
        if args.model:
            overrides['model_alias'] = args.model
        if args.memory:
            overrides['memory'] = True
        if overrides:
            settings = agent_config.AgentConfig.from_dict(
                {**settings.to_dict(), **overrides}
            )
        memory_path = Path(args.memory_path) if args.memory_path else None
        task = None if args.folder is None else _task(args)
        candidates = () if task is None else task.candidate_services
        model: contracts.ModelClient = (
            fake_policy.RuleClient(candidates)
            if args.fake
            else _paid_client(args, settings)
        )
        if args.continue_run is not None:
            result = continuation.continue_run(
                Path(args.continue_run),
                [Path(path) for path in args.add],
                model,
                settings,
                memory_path=memory_path,
            )
        elif task is not None:
            runs_dir = (
                Path(args.runs_dir)
                if args.runs_dir
                else (DRY_RUNS_DIR if args.fake else RUNS_DIR)
            )
            result = graph.run_investigation(
                task,
                model,
                settings,
                runs_dir=runs_dir,
                memory_path=memory_path,
            )
    except (config.ConfigError, OSError, ValueError) as exc:
        parser.error(str(exc))
    final = result.report
    print(f'Run: {result.run_dir}')
    print(f'Outcome: {final.outcome} (stop: {final.stop_reason})')
    print(f'Ranks: {", ".join(final.ranks) or "none"}')
    if result.run_dir is not None:
        print(f'Report: {result.run_dir / "report.md"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
