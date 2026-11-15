# Parallel Build Plan

Which parts of the build can be done at the same time, which cannot, and what has to be
agreed first to make the parallel parts safe. Phases are numbered in dependency order in
the [planning hub](00-high-level-architecture-planning.md); this document is the
finer-grained view, down to workstreams.

See [`flowchart-build-waves.svg`](flowchart-build-waves.svg).

## 1. Summary

- The build falls into **six waves**. Everything inside a wave can be built at the same
  time, on separate branches, each with its own PR.
- At most **four streams** are useful at once (wave 3). Waves 1 and 6 are single-stream.
- Three phases split across waves: **06**, **08** and **09** each have parts that can
  start well before the rest of the phase is unblocked.
- **Paid runs are never parallel.** They share one budget and each informs the next.
- Parallel work is safe only because five **contracts** are fixed early (§4). A stream
  that needs to change a contract stops being independent.

## 2. Waves

| Wave | Streams that can run in parallel | Needs first |
|---|---|---|
| 1 | 01 Foundation | — |
| 2 | 02 Baseline and split · 03 Ingest and tools (deterministic part) · 04 Model layer | 01 |
| 3 | 05 Agent · 06-B Scoring and statistics · 06-D Remaining field-set cases · 03 Model-assisted classifier | 02, 03, 04 |
| 4 | 06-A/C Runner and scripts · 08 Upload app, single mode · 09 Memory store, retrieval and tool | 05 |
| 5 | 07 Multi-agent · 08 Role lanes and feedback form · 09 Memory in multi mode and seeding script | 06 runner; wave 4's app and memory |
| 6 | 10 Verification and report | all |

## 3. Stream by stream

For each stream: what it needs before it can start, what it can run beside, and the
shared files where it can collide with a neighbour.

### Wave 1

| Stream | Needs | Notes |
|---|---|---|
| **01 Foundation** | — | Single stream on purpose: it creates `pyproject.toml`, the `Makefile`, `config/`, the package skeleton, CI and the smoke gate that every other stream builds on. Splitting it produces conflicts in exactly those files. |

### Wave 2

| Stream | Needs | Parallel with | Shared files |
|---|---|---|---|
| **02 Baseline and split** | 01 | 03, 04 | `Makefile` |
| **03 Ingest and tools** (Workstreams A and B, without the model fallback) | 01 | 02, 04 | `pyproject.toml` (DuckDB) |
| **04 Model layer** | 01 | 02, 03 | `pyproject.toml` (LiteLLM), `config/` |

These three share no source directories: `eval/` and `splits/`, `ingest/` and `tools/`,
and `llm/`. Inside 03, ingest (A) and tools (B) can also be split between two people
once the catalog schema is agreed, since tools only read what ingest writes.

Order inside 02 is fixed: the split is locked **before** the headroom analysis reads any
label. That part is sequential.

### Wave 3

| Stream | Needs | Parallel with | Shared files |
|---|---|---|---|
| **05 Agent** (ledger, graph, run record, walking skeleton) | 03, 04 | all of wave 3 | `agent/prompts.py` |
| **06-B Scoring and statistics**, including the evaluator contract test | 02 | all of wave 3 | — |
| **06-D Remaining four field-set cases** and their scorer | 03 | all of wave 3 | `evalsets/field/` |
| **03 Model-assisted classifier** | 03, 04 | all of wave 3 | `ingest/` |

06-B needs only RCAEval, the split and prediction files, which BARO already produces in
wave 2, so it does not wait for the agent. The same holds for the agreement statistic
and for `eval_onset.py`, which needs only `find_onset` from Phase 03. 06-D needs only ingest, to check that the
generated cases load.

Inside 05, the ledger (Workstream A) and the graph (B) are sequential: the graph is
written against the ledger. The run record (C) can proceed beside B once the trajectory
schema is fixed.

### Wave 4

| Stream | Needs | Parallel with | Shared files |
|---|---|---|---|
| **06-A/C Runner and scripts** (`run_eval.py`, `compare_models.py`, `failure_report.py`, the single-call baseline) | 05, 06-B | 08, 09 | `Makefile`, `scripts/` |
| **08 Upload app, single mode**, including the past-runs viewer | 05 | 06, 09 | `Makefile` |
| **09 Memory store, retrieval, tool and frozen mode** | 05 | 06, 08 | `tools/` registry |

All three consume the agent through the same run function and trajectory, and none
changes it. The past-runs viewer needs only saved trajectories, so it can start as soon
as the trajectory schema is fixed.

### Wave 5

| Stream | Needs | Parallel with | Shared files |
|---|---|---|---|
| **07 Multi-agent** | 05 merged; 06 runner | 08 and 09 leftovers | `agent/prompts.py`, run state |
| **08 Role lanes, feedback form, add-files-and-continue** | 08 from wave 4; role tags in the trajectory (07); memory store (09) | 07 once its trajectory tags are agreed | `app/` |
| **09 Memory in multi mode, seeding script** | 09 from wave 4; 07 for the lead-only rule; 02 for tuning answers | 07, 08 | `agent/` |

The two leftover streams depend on small, nameable pieces of 07 (the role tag in the
trajectory, the lead-only rule). If those are agreed at the start of the wave, all three
proceed together; otherwise the leftovers follow 07.

### Wave 6

| Stream | Needs | Notes |
|---|---|---|
| **10 Verification and report** | everything | Single stream. The final run, the fresh-clone rehearsal and the write-up each depend on the finished system, and the report is assembled from every phase file. |

## 4. Contracts that make parallel work safe

Each is a small, written interface. It is fixed by the stream that owns it, early in its
wave, before the streams that consume it start.

| Contract | Owner | Fixed by | Consumed by |
|---|---|---|---|
| Split file and answers subsets (`splits/re2-ob.json`, `*.answers.jsonl`) | 02 | start of wave 2 | 06, 09, 10 |
| Catalog schema and tool result shape (including locators) | 03 | middle of wave 2 | 05, 06-D, 08 |
| Model client interface and the scripted fake | 04 | middle of wave 2 | 03 classifier, 05, 07, every test |
| Run function signature and trajectory schema | 05 | start of wave 3 | 06-A, 08, 09, 07 |
| Prediction record format | RCAEval (external) | given | 02, 06 |

Fixing the run function and trajectory schema at the **start** of wave 3, with the fake
client behind it, lets wave 4's runner and app begin against a stub while the agent is
still being built. That is the main way to shorten the critical path.

## 5. What cannot be parallelised

| Item | Why |
|---|---|
| **Paid runs** | One shared budget, and each run's result decides the next. Order: pilot → noise check → tuning passes → single-call baseline → field set → ablation → single versus multi → model comparison → final run (Phase 06, Decision 6). Parallel workers *within* one run are fine; the spend guard admits their calls one reservation at a time. |
| **Split before labels** | The split must be locked before anything reads a labelled result, or held-out cases influence the design. |
| **Phase 01** | It creates the files every other stream edits. |
| **05 before 07** | Both edit the prompts module and the run state; 07's testers reuse 05's investigate function. 07 starts after 05 is merged. |
| **Ledger before graph, inside 05** | The graph's nodes are written against the ledger's rules. |
| **Tuning before the final run** | The configuration and mode are frozen on tuning results; the held-out set is run once, after. |
| **Phase 10** | It verifies and reports on the whole system. |

## 6. Rules for working in parallel

1. **One branch and one PR per stream**, named `feature/phase-NN-<stream>`.
2. **Shared files merge one at a time.** `pyproject.toml`, the `Makefile`, `config/`,
   the tool registry and the SVGs are touched by several streams; whoever merges second
   rebases. Keep changes to them in small, separate commits so rebasing is mechanical.
3. **A contract change is announced before it is made.** If a stream needs to change one
   of the contracts in §4, every consuming stream is checked first, and the change lands
   in its own PR.
4. **Stubs are replaced, not kept.** A stream that starts early against a stub removes
   the stub in the PR that wires in the real component.
5. **No paid run from a feature branch.** Paid runs happen from merged code, in priority
   order, so a result always corresponds to a commit on `main`.
6. **Each stream updates its own phase file.** Two streams in the same phase (for
   example 06-B and 06-D) edit different checklist sections, so they do not conflict.

## 7. Critical path

The longest chain that nothing can shorten by adding streams:

**01 → 03 → 05 → 06 runner → 07 → 10**

Everything else has slack: 02 and 04 finish beside 03; scoring, field-set cases, the app
and the memory store all fit alongside the chain. Time saved on any step in the chain
shortens the whole build; time saved elsewhere does not.
