"""Tuning-set headroom report and agent cost estimate. Spends nothing.

Reads the per-case tuning rows written by `run_baselines.py`, writes
aggregate breakdowns by fault and by service to `results/phase-02/`, and
keeps the miss lists in gitignored `runs/phase-02/`. Held-out cases are
never read.

The cost estimate measures RCAEval's reference prompt and tool results on
the tuning cases and prices them with LiteLLM's bundled price map, read
offline.
"""

import json
import os
import pathlib
import statistics
from collections.abc import Mapping
from collections.abc import Sequence

from incidents_agent.eval import headroom
from incidents_agent.eval import rcaeval_bridge
from incidents_agent.eval import split as split_lib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
DATASET = 're2-ob'
CASES_DIR = _ROOT / 'agent_tasks' / DATASET / 'cases'
RUNS_DIR = _ROOT / 'runs' / 'phase-02'
RESULTS_DIR = _ROOT / 'results' / 'phase-02'


def _read_rows(model: str) -> list[dict[str, object]]:
    path = RUNS_DIR / f'{model}_tuning.rows.jsonl'
    lines = path.read_text(encoding='utf-8').splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _table(title: str, scores: Mapping[str, headroom.GroupScore]) -> str:
    lines = [f'| {title} | n | AC@1 | AC@3 |', '|---|---|---|---|']
    lines += [
        f'| {value} | {s.n} | {s.ac1:.2f} | {s.ac3:.2f} |'
        for value, s in scores.items()
    ]
    return '\n'.join(lines)


def breakdown_report(models: Sequence[str]) -> str:
    """Write breakdowns and miss lists; return the Markdown report.

    Args:
        models: Prediction sets to break down, by file stem.

    Returns:
        The Markdown text written to `results/phase-02/`.
    """
    sections = ['# Phase 02 tuning-set breakdown (service level)', '']
    for model in models:
        rows = _read_rows(model)
        sections += [
            f'## {model}',
            '',
            _table('Fault', headroom.breakdown(rows, 'fault')),
            '',
            _table('Service', headroom.breakdown(rows, 'service')),
            '',
        ]
        missed = {
            'top1': headroom.misses(rows, 1),
            'top3': headroom.misses(rows, 3),
        }
        (RUNS_DIR / f'{model}_tuning_misses.json').write_text(
            json.dumps(missed, indent=2) + '\n', encoding='utf-8'
        )
        sections += [
            f'Misses: {len(missed["top1"])} at top 1, '
            f'{len(missed["top3"])} at top 3, of {len(rows)} cases.',
            '',
        ]
    return '\n'.join(sections)


def _prices(model: str) -> headroom.Prices:
    # Imported here, after forcing LiteLLM to read its bundled price map
    # instead of fetching one over the network.
    os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
    import litellm

    entry = litellm.model_cost[model]
    cache = entry.get('cache_read_input_token_cost')
    return headroom.Prices(
        input=float(entry['input_cost_per_token']),
        output=float(entry['output_cost_per_token']),
        cache_read=None if cache is None else float(cache),
    )


def cost_report(
    config: headroom.CostConfig, case_ids: Sequence[str]
) -> dict[str, object]:
    """Measure prompt and tool-result sizes and price one case per model.

    Args:
        config: The cost-estimate configuration.
        case_ids: Tuning case ids to measure on.

    Returns:
        The measurements, assumptions and per-model estimates.
    """
    result_chars = []
    prompt_chars = []
    for case_id in case_ids:
        case_dir = CASES_DIR / case_id
        result_chars += rcaeval_bridge.tool_result_chars(
            case_dir, config.probe_calls
        )
        prompt_chars.append(rcaeval_bridge.reference_prompt_chars(case_dir))
    cap = config.result_cap_chars
    capped = [min(chars, cap) for chars in result_chars]
    sizes = {
        'typical': int(statistics.mean(capped)),
        'heavy': int(statistics.quantiles(capped, n=10)[-1]),
    }
    prompt_tokens = headroom.tokens_from_chars(
        int(statistics.mean(prompt_chars))
    )
    shapes = {
        name: headroom.LoopShape(
            prompt_tokens=prompt_tokens,
            result_tokens=headroom.tokens_from_chars(chars),
            output_tokens=config.output_tokens_per_turn,
            tool_calls=config.tool_calls,
        )
        for name, chars in sizes.items()
    }
    estimates: dict[str, dict[str, object]] = {}
    for model in config.models:
        prices = _prices(model)
        estimates[model] = {}
        for name, shape in shapes.items():
            estimate = headroom.estimate_case_cost(shape, prices)
            estimates[model][name] = {
                'input_tokens': estimate.input_tokens,
                'uncached_usd': round(estimate.uncached_usd, 4),
                'cached_usd': None
                if estimate.cached_usd is None
                else round(estimate.cached_usd, 4),
            }
    return {
        'measured_on': f'{len(case_ids)} tuning cases',
        'probe_results': len(result_chars),
        'mean_prompt_chars': int(statistics.mean(prompt_chars)),
        'result_chars_after_cap': {
            'mean': sizes['typical'],
            'median': int(statistics.median(capped)),
            'p90': sizes['heavy'],
        },
        'loop': {
            'prompt_tokens': prompt_tokens,
            'result_tokens': {
                name: shape.result_tokens for name, shape in shapes.items()
            },
            'output_tokens_per_turn': config.output_tokens_per_turn,
            'tool_calls': config.tool_calls,
            'chars_per_token': headroom.CHARS_PER_TOKEN,
        },
        'target_usd_per_case': config.target_usd_per_case,
        'estimates': estimates,
    }


def main() -> None:
    """Write the tuning breakdown and the cost estimate."""
    record = json.loads(
        (_ROOT / 'splits' / f'{DATASET}.json').read_text(encoding='utf-8')
    )
    split = split_lib.from_json(record)
    models = sorted(
        p.name.removesuffix('_tuning.rows.jsonl')
        for p in RUNS_DIR.glob('*_tuning.rows.jsonl')
    )
    report = breakdown_report(models)
    (RESULTS_DIR / 'tuning_breakdown.md').write_text(report, encoding='utf-8')
    print(report)

    config = headroom.parse_cost_config(
        json.loads(
            (_ROOT / 'config' / 'cost_estimate.json').read_text(
                encoding='utf-8'
            )
        )
    )
    cost = cost_report(config, split.tuning)
    (RESULTS_DIR / 'cost_estimate.json').write_text(
        json.dumps(cost, indent=2) + '\n', encoding='utf-8'
    )
    print(json.dumps(cost, indent=2))


if __name__ == '__main__':
    main()
