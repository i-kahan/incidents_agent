# Incidents Agent

Incidents Agent investigates service incidents from metrics, logs, traces, and
other telemetry. It ranks possible root causes and cites the evidence behind its
diagnosis. See the [design](DESIGN.md) and [implementation plan](planning/).

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

Offline checks use synthetic cases. The paid memory experiment remains deferred
to Phase 10. Multi-graph integration awaits Phase 07; the shared loop already
restricts memory lookup and re-reading to `agent` (single mode) and `lead` roles.
