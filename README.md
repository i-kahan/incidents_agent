# Incidents Agent

Incidents Agent investigates service incidents from metrics, logs, traces, and
other telemetry. It ranks possible root causes and cites the evidence behind its
diagnosis. See the [report](REPORT.md), [design](DESIGN.md), and
[implementation plan](planning/00-high-level-architecture-planning.md).

![System overview](planning/flowchart-system-overview.svg)

## Held-out results (60 RE2-OB cases)

All paid arms served `gpt-4o-mini-2024-07-18`; intervals are 95% pair-resampled
bootstraps. See [REPORT.md](REPORT.md) and
[results/final/comparison.md](results/final/comparison.md) for paired comparisons,
field fixtures (`1/7` passed), onset accuracy, human audit, and failure analysis.

| Arm / Baseline | Service AC@1 (95% CI) | Service AC@3 | Service AC@5 | Cost / case | Delta vs. BARO |
|---|---:|---:|---:|---:|---:|
| `guarded_baro` *(free)* | **0.750** `[0.583, 0.900]` | 0.867 | 0.917 | `$0.0000` | `+0.533` |
| `memory` *(ours + tuning seed)* | **0.717** `[0.567, 0.850]` | 0.817 | 0.817 | `$0.0099` | `+0.500` |
| `primary` *(ours, single)* | **0.617** `[0.467, 0.750]` | 0.750 | 0.750 | `$0.0094` | `+0.400` |
| `multi` *(ours, multi-agent)* | **0.617** `[0.450, 0.767]` | 0.850 | 0.850 | `$0.0070` | `+0.400` |
| `rcaeval-reference` *(LLM)* | **0.617** `[0.433, 0.783]` | 0.850 | 0.917 | `$0.0026` | `+0.400` |
| `single-call` *(1-call LLM)* | **0.283** `[0.117, 0.483]` | **0.917** | 0.983 | `$0.0007` | `+0.067` |
| `mmbaro_candidates` *(free)* | **0.233** `[0.067, 0.417]` | 0.817 | **1.000** | `$0.0000` | `+0.017` |
| `mmbaro` *(free)* | **0.233** `[0.067, 0.417]` | 0.817 | 0.950 | `$0.0000` | `+0.017` |
| `baro` *(free)* | **0.217** `[0.067, 0.400]` | 0.817 | 0.917 | `$0.0000` | `0.000` |

## Quickstart

Install [uv](https://docs.astral.sh/uv/) and run:

```sh
uv python install
uv sync --locked --group benchmark
make app
```

Open the localhost URL printed by Streamlit. Use the app’s free demo mode to
try an investigation without a provider key. For a live run, copy
`.env.example` to `.env` and configure the key for your model of choice from
`config/models.yaml`. The default is the OpenAI alias `gpt-small`.

To export the RCAEval benchmark and rehearse the evaluation without spending:

```sh
make export
make split
make baselines
make eval EVAL_ARGS='--cases tuning --limit 3 --dry-run'
```

The first export downloads the benchmark. To reuse an existing extracted
RE2-OB dataset, replace `make export` with:

```sh
uv run --locked --group benchmark rcaeval-agent export --dataset re2-ob \
  --out agent_tasks --data-root /absolute/path/to/RE2-OB
```

## Published replay (no API keys required)

With `results/final/replay.json` committed, start the app and select **Past
Runs → Published results**. The viewer replays the saved report, steps, and
citations without provider keys or the original evaluation cache. To replay a
trajectory in the terminal:

```sh
make replay ARGS='results/final/trajectories/primary/re2-ob-0001/trajectory.jsonl'
```

## Frozen incident memory

Memory is off by default. Build a reproducible seed from the locked tuning split:

```sh
uv run --locked python -m scripts.seed_memory
```

The default bundle is `runs/phase-09/memory-seed/` in the shared repository,
containing `memory.sqlite3` and `manifest.json`. `--split`, `--tasks-root`,
`--answers`, and `--out` override the inputs and destination. The script reads only
tuning telemetry and tuning answers, makes no model calls, and refuses existing
output directories. Fixes and recommended actions remain empty: benchmark labels
establish a cause and fault, not a verified repair.

Evaluation accepts `--memory --memory-path /absolute/path/to/memory-seed` on
`run_eval.py` and `compare_models.py`. It validates the seed against the split,
copies it privately, and opens that copy read-only. The content hash is part of
the cache key. Evaluation cases must be disjoint from the seed IDs; the full seed
therefore cannot be used to evaluate the tuning cases it contains. Existing
`--final` and paid-run admission rules still apply. The single-call baseline does
not accept memory.

## License & attribution

The project uses the [MIT license](LICENSE). See [NOTICE.md](NOTICE.md) for
RCAEval benchmark attribution and citations.
