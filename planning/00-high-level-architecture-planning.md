# 00 — High-Level Architecture & Planning Overview

Planning hub for the Incidents Agent build. Each phase below has its own file with a
status, checklist, decision log, the constraints that apply to it, and AI-use notes.
These docs are structured to roll up into the final `REPORT.md` (see §6). The full
design lives in [`DESIGN.md`](../DESIGN.md); this folder is the plan for building it.

**Decisions vs. constraints:** a *decision* is a choice with a defensible alternative —
it earns a `Decision / Context / Trade-off` triplet in a phase file's §3 and a line in
the trade-offs section of `REPORT.md`. A *constraint* is something fixed from outside
the phase: by the owner (time, budget, style guide) or by the benchmark (RCAEval's task
format and scoring). It is recorded in the phase file's §4 with its source and its
implication, and it is never presented as a trade-off.

**Optional fourth bullet — `Rejected alternatives`:** a triplet may name an option that
was seriously considered and turned down, with the concrete reason it fails *in this
system*. Reserve it for alternatives that are attractive on their face and would
otherwise be re-litigated.

## 1. System Architecture

```
 upload folder ─┐                                        ┌─> incident report (UI)
                ├─> Ingest ─> DuckDB + catalog ─> Agent ─┤
 RCAEval case ──┘                 ▲                │     └─> predictions.jsonl ─> Eval
                                  └──── Tools ◄────┘
                                          ▲
                              Incident memory (optional)
```

- **One agent, two entry points:** the Streamlit upload page and the eval runner both
  hand the agent a folder of files plus an optional task.
- **The agent never sees answers:** RCAEval labels live in a separate answers file that
  only the evaluator and `seed_memory.py` read.
- **Two agent architectures, one flag:** a single agent, and a multi-agent mode with a
  lead, parallel hypothesis testers and an independent skeptic. Both use the same
  tools, ledger, limits and reporting.
- **All model calls go through one layer:** LiteLLM, with a spend guard that stops every
  run at a configurable dollar limit ($15 by default, less a $1 safety margin).

Diagrams (kept in this folder, see §8):

| Diagram | Shows |
|---|---|
| [`flowchart-system-overview.svg`](flowchart-system-overview.svg) | Every component and how data moves between them |
| [`flowchart-agent-graph.svg`](flowchart-agent-graph.svg) | The single agent's nodes, the ledger, limits and the grounding check |
| [`flowchart-multi-agent.svg`](flowchart-multi-agent.svg) | The lead, the hypothesis testers and the independent skeptic |
| [`flowchart-eval-harness.svg`](flowchart-eval-harness.svg) | From the dataset to the comparison table |

## 2. Guiding Principles

1. **Measure first.** The headroom study (Phase 02) runs before the agent is built. If
   the baseline leaves little room, the target setting changes before any design is
   locked in.
2. **Evaluation is the product.** The agent is only as credible as the numbers behind
   it: held-out cases, confidence intervals, paired comparisons, and a failure analysis.
3. **Honest over flattering.** If the agent does not beat BARO, the report says so. No
   result is written down before it has been produced.
4. **Schema-agnostic by construction.** Tools work on whatever files were dropped. An
   RCAEval case is just one more folder; nothing in the agent is special-cased to it.
   A small field set of other-shaped incidents keeps that claim honest.
5. **Budget is a hard limit.** The amount is configuration, $15 by default. The spend
   guard enforces it in code, and every
   paid run is cached so nothing is paid for twice.
6. **Explainability.** Every decision is logged in a phase file so it can be defended
   and lifted into `REPORT.md`.

## 3. Phase Index & Dependencies

| # | Phase file | Depends on | Status |
|---|---|---|---|
| 01 | [Foundation & Environment](01-foundation-and-environment.md) | — | Not started |
| 02 | [Baseline & Headroom Study](02-baseline-and-headroom-study.md) | 01 | Not started |
| 03 | [Ingest & Tools](03-ingest-and-tools.md) | 01 (its optional model fallback: 04) | Not started |
| 04 | [Model Layer](04-model-layer.md) | 01 | Not started |
| 05 | [Agent](05-agent.md) | 03, 04 | Not started |
| 06 | [Evaluation Harness](06-evaluation-harness.md) | 02, 05 | Not started |
| 07 | [Multi-Agent Investigation](07-multi-agent.md) | 05, 06 | Not started |
| 08 | [Upload App](08-upload-app.md) | 05, 07 | Not started |
| 09 | [Incident Memory](09-incident-memory.md) | 05, 06, 07, 08 | Not started |
| 10 | [Verification & Report](10-verification-and-report.md) | all | Not started |

**Ordering rationale:** the baseline (02) comes before the agent so the target is known.
Ingest/tools (03) and the model layer (04) are independent of each other and can be
built in parallel, except for the optional model-assisted file classifier in 03, which
is wired in after 04. A cited report for one case, produced from the command line with
the fake client, is working at the end of 05, before any fan-out is built. The evaluation harness (06) comes before multi-agent (07) because the
comparison between the two architectures needs it, and the single agent must exist
first as the baseline. The app (08) follows so it can show either mode. Memory (09)
comes last among the features because it plugs into both modes and into the app's
feedback form.

**There is no deadline; the budget is the limit.** Every feature in the plan is built.
Every paid use is ranked in Phase 06, Decision 6. The final held-out run is first and its
money is held in a named reserve in the spend guard; everything else is spent in priority
order and dropped from the bottom if the budget runs out.

## 4. Decision Log (summary — details live in the phase files)

| Decision | Choice | Phase |
|---|---|---|
| Tooling | `uv` for environments, `ruff` for lint and format, `mypy` (strict) for types, `pytest` for tests | 01 |
| Workflow | One feature branch and one PR per phase (or workstream); squash merge | 01 |
| Benchmark target | RCAEval RE2, Online Boutique, unless the headroom study says otherwise | 02 |
| Data split | Locked before any labelled result is examined; grouped by service–fault pair, 10 pairs (30 cases) tuning / 20 pairs (60 cases) held out | 02 |
| Benchmark target rule | Changed only if BARO's top-1 on tuning is 0.90 or higher, by a rule fixed in advance | 02 |
| Baselines | BARO, plus multi-source BARO if it runs, to separate better reasoning from richer inputs | 02 |
| Storage for dropped files | One DuckDB database per run, read-only, external access disabled | 03 |
| Tool surface | A few generic tools (SQL, text search, window comparison) instead of RCAEval-specific ones | 03 |
| Documents | Chunked by structure (headings, hunks, sections), keyword-searched, citable by chunk id | 03 |
| Choosing the chunking | Deterministic rules; one small model call, from a fixed menu, only for ambiguous files | 03 |
| Own tools vs. RCAEval's `CaseToolkit` | Own tools; RCAEval's reference agent is a baseline, not a foundation | 03 |
| Model access | LiteLLM; OpenAI, Anthropic and Google configured by the owner in `config/models.yaml` | 04 |
| Unknown-price models | Refused unless a price override is set, so the spend guard always works | 04 |
| Budget amount | Configuration (`config/budget.yaml`, overridable per invocation), recorded in every run; never coded | 04 |
| Spend guard | Admission rule: reserve worst-case cost before each call, settle after; named reserve for the final run | 04 |
| Agent framework | LangGraph with four nodes: orient, investigate, verify, report | 05 |
| Working state | Hypothesis ledger; citation validity enforced in code (successful call, locator, quoted value) | 05 |
| Evidence quality | Reported separately from citation validity, by a manual audit of a sample | 05, 06 |
| Forced verification | At least one check that could disprove the top hypothesis; its capacity is reserved before investigation | 05 |
| Confidence | Qualitative level with reasons; no number unless calibrated | 05 |
| Scoring | RCAEval's official evaluator for headline numbers; own statistics layered on top | 06 |
| Run caching | Keyed by a fingerprint of model settings, prompts, tools, limits, mode, source, case contents and memory snapshot | 06 |
| Held-out gate | Applied to the resolved case list, so no option or script can bypass it | 06 |
| Uncertainty | Intervals and paired differences bootstrapped by service–fault pair | 06 |
| Budget | Every paid use ranked; the final run's money reserved first | 06 |
| Model comparison | `run_eval.py` runs one configuration; `compare_models.py` aggregates several | 06 |
| Upload-path evidence | Six hand-built non-RCAEval incidents, scored deterministically, reported separately as counts | 06 |
| Multi-agent shape | Lead, parallel hypothesis testers, independent skeptic; split by hypothesis, not by data source | 07 |
| Multi-agent comparison | Same total budget per case as the single agent; equal-budget result is the headline | 07 |
| Architecture for the final run | Multi only if the lower bound of its paired 95% interval for top-1 gain on tuning is above zero; otherwise single | 07 |
| UI | Streamlit, local only | 08 |
| Memory retrieval | BM25 keyword search plus component matching; no embeddings for now | 09 |
| Memory in evaluation | Off by default; frozen (read-only) and fingerprinted during the memory experiment | 09 |
| Memory output | Confirmed records as leads, rejected ones as counterevidence, unverified only as a last resort | 09 |
| Final write-up | `REPORT.md`, produced by aggregation in Phase 10 | 10 |

Open items, flagged **⚠️ OPEN** in the phase files:

- **Python version and RCAEval install** (01): RCAEval's supported versions are
  unchecked.
- **Target setting** (02): confirmed or changed once BARO's RE2-OB scores are measured.
- **Models to compare** (04, 06): the owner supplies keys and model IDs.

## 5. Constraints (summary)

| Constraint | Source | Phase |
|---|---|---|
| No deadline; every planned feature is built | Owner | all |
| A configurable dollar limit on LLM spend, $15 by default; mostly cheap models | Owner | 04, 06 |
| Google Python Style Guide for all Python | Owner | all |
| pytest for all tests | Owner | all |
| OpenAI, Anthropic and Google supported; the owner chooses keys and models | Owner | 04 |
| A script to compare models | Owner | 06 |
| Drop-files platform as the end product | Owner | 03, 08 |
| Supporting documents are usable input, chunked and citable | Owner | 03 |
| A non-benchmark test set for the upload path | Owner | 06 |
| Multi-agent investigation, alongside the single agent | Owner | 07 |
| Long-term incident memory, off for headline numbers, with an optional experiment | Owner | 09 |
| Final write-up must not be named `SOLUTION.md` | Owner | 10 |
| Blind task view, closed answer vocabulary, `predictions.jsonl` format | RCAEval `docs/AGENTS.md` | 02, 06 |
| Fault start time is given in each task | RCAEval `task.json` | 02, 05 |

## 6. How these docs roll up into `REPORT.md`

`REPORT.md` is written only in Phase 10, by aggregation. Nothing is drafted before the
work is real.

| `REPORT.md` section | Source |
|---|---|
| **How to run it** | The run steps in the phase checklists |
| **What was built / what's left** | Phase objectives and final statuses; unchecked boxes become "what's left" |
| **Results** | Phase 02, 06, 07, 09 and 10 verification evidence, copied from committed result files |
| **Trade-offs** | The `Decision / Context / Trade-off` triplets from every phase §3 — decisions only |
| **Where AI tools were used** | The AI-use notes kept in each phase file as work happens |

## 7. Development Conventions (apply to every phase)

1. **Tests ship with the code.** Rules for when tests are required and what they look
   like are in [`docs/practices/testing.md`](../docs/practices/testing.md). pytest only.
2. **One PR per phase or workstream.** What a PR must contain is in
   [`docs/practices/pull-requests.md`](../docs/practices/pull-requests.md).
3. **Code style.** [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html); a local
   copy is kept out of git. Enforced by
   `ruff` where it can be. Types are checked by `mypy` in strict mode.
4. **Planning stays in sync with the code.** Whenever code is written, the owning phase
   file is updated in the same PR: status, checked boxes, new or changed decisions, and
   an AI-use note. A stale planning doc is treated as a bug.
5. **Diagrams stay in sync with the architecture.** A PR that adds, removes or rewires a
   component updates the affected SVG in the same PR (see §8).

## 8. Architecture Diagrams

The four SVGs in §1 are hand-written and versioned with the code.

- **Style:** one shared palette and font, light and dark themes via
  `prefers-color-scheme`, a `<title>` and `<desc>` for accessibility, and a legend.
- **Ownership:** Phase 01 confirms the first versions against the scaffolded repo. Each
  later phase has a checklist item to update the diagram it affects. Phase 10 does a
  final pass and embeds them in `README.md` and `REPORT.md`.
- **Additions:** a phase may add a diagram when a workstream needs one; it is named
  `flowchart-<subject>.svg` and listed in the table in §1.
