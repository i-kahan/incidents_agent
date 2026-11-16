# Incidents Agent — Design

An AI agent that investigates incidents from dropped files (logs, metrics, traces and
supporting data), forms and tests hypotheses, and reports a root cause with cited evidence.
It is evaluated on the RCAEval benchmark against a statistical baseline.

See the [phase index](planning/00-high-level-architecture-planning.md#3-phase-index--dependencies)
for implementation status.

The build plan is in [`planning/`](planning/00-high-level-architecture-planning.md): a
hub document, one file per phase, and the architecture diagrams. Working practices are
in [`docs/practices/`](docs/practices/). The final write-up will be
[`REPORT.md`](REPORT.md). What comes after the plan is in
[`planning/future-improvements.md`](planning/future-improvements.md).

## Goals and constraints

- **Emphasis:** agent design and measured evaluation.
- **LLM spend:** a configurable dollar limit, $15 by default, mostly cheap models. A guard
  stops all runs $1 short of it. The budget, not a deadline, is what limits the work.
- **Out of scope:** executing remediation, live cluster demo, authentication, deployment.

## Overview

```
 upload folder ─┐                                        ┌─> report (UI)
                ├─> Ingest ─> DuckDB + catalog ─> Agent ─┤
 RCAEval case ──┘                 ▲                │     └─> predictions.jsonl ─> Eval
                                  └──── Tools ◄────┘
                                          ▲
                                   Incident memory (optional)
```

One agent serves two entry points: the upload page and the benchmark runner. Both hand it
a folder of files plus an optional task (incident time, description, candidate answers).

## 1. Ingest

Turns a folder into a read-only DuckDB database and a catalog the agent reads first.

- **Tabular files** (CSV, Parquet, JSON lines): one table each.
- **Text logs:** one table with line number, raw text, and timestamp and level where they
  can be detected.
- **Documents** (runbooks, notes, diffs, config files): chunked along their own
  structure and stored in a `document_chunks` table with a heading path per chunk.
  Markdown and prose split by heading then paragraph; diffs by file and hunk; config
  files by top-level section; small documents kept whole.
- **Classification:** each file is classed as tabular, log or document by extension and
  then content; the upload page shows the result and lets the user override it.
- **Model-assisted fallback:** when the rules are unsure about a file, one small model
  call picks its kind and chunking strategy from a fixed menu. The choice is cached by
  file hash and recorded in the catalog. Files the rules can place never use it.
- **Catalog:** per table, the columns, types, row count, detected time column, time range
  and three sample rows; per document, its title, type, size and outline.
- **Limits:** a file-size cap. Unsupported files are listed as skipped; they do not fail
  the run.

## 2. Tools

Plain Python functions, independent of the agent framework. Every result is capped at
about 6,000 characters with a truncation notice. Errors are returned as data so the agent
can react to them.

| Tool | Purpose |
|---|---|
| `list_tables` | Show the catalog |
| `describe_table` | Columns, basic statistics, sample rows |
| `run_sql` | Read-only query with a row limit and timeout |
| `search_text` | Regex over log text, grouped by message pattern, with counts before and after a time |
| `compare_windows` | Rank numeric columns by how much they shift across a split time |
| `time_series` | One column downsampled into buckets |
| `find_onset` | Estimate when the incident started, for inputs with no known time |
| `dependency_graph` | Who calls whom, with how each edge changed across the incident time; says so when the data has no relationships |
| `search_documents` | Keyword search over document chunks |
| `read_document` | One chunk with its neighbours, or a document's outline |
| `search_past_incidents` | Similar past incidents from memory (only when memory is on) |
| `update_hypotheses` | Add or revise ledger entries |
| `submit` | Final ranked diagnosis, or "inconclusive" |

`compare_windows` is the same kind of statistic the BARO baseline uses. The agent's added
value has to come from cross-checking it against logs and traces and from separating the
root cause from services that only show symptoms.

## 3. Agent

A LangGraph graph with four nodes:

1. **Orient** (no LLM): builds the briefing from the catalog and task.
2. **Investigate:** the LLM calls tools in a loop and maintains the ledger.
3. **Verify:** before finishing, the LLM must run at least one check that could disprove
   its top hypothesis (for example, whether the suspect is upstream of the other affected
   services or merely affected by them).
4. **Report:** structured output.

### Hypothesis ledger

Per-run working state, not long-term memory. Each entry has:

- a statement and the suspected component
- a status: open, supported or rejected
- a qualitative confidence (low / medium / high)
- evidence for and against, each item citing a tool call, a locator in its result and a
  quoted value
- a verification outcome: supported, refuted, inconclusive or unverified
- an origin tag: `data` or `memory`

Checks enforced in code:

- **Citation validity:** every evidence item must reference a tool call from this run
  that succeeded with a non-empty result, and quote a value that appears in it. The
  final report is validated the same way.
- Citation validity shows a claim points at something real. Whether the observation
  supports the claim is checked by a manual audit of a sample, and reported separately.
- A hypothesis with origin `memory` cannot be marked supported without evidence from the
  current data.

### Output

Ranked root causes, fault type, verification status, qualitative confidence, cited
evidence, recommended actions (never executed) and cost. No numeric probability is shown
unless it has been calibrated. For benchmark cases the ranks are validated against the candidate
list RCAEval supplies.

### Limits

Per case: about 15 tool calls, plus caps on model turns, wall time and cost. Capacity for
the verify step and one correction is reserved before investigation starts. Every way a
run can end goes through one finalisation path that renders the ledger into a ranking
marked verified, provisional or inconclusive, with the stop reason. `no_fault_found` is
a valid outcome, distinct from inconclusive.

### Context policy

Only the most recent tool results stay in the conversation in full; older ones become
short deterministic digests. The ledger is the durable summary, and full results stay in
the trajectory so citations remain checkable. The stable part of the prompt comes first,
to use provider prompt caching where available. This is what keeps cost per case down.

### Data requests and continuing a run

When the outcome is provisional or inconclusive, the report lists the specific data that
would settle it, each tied to an open hypothesis. Files can then be added and the same
run continued, with its ledger and evidence carried over. The agent cannot fetch data
itself.

### Recommended actions

Each action is tied to the hypothesis it addresses and carries a risk level (read-only
check, reversible change, disruptive change) and how to verify it worked. None is
executed.

### Ablation switch

Ledger and verify can be turned off, leaving a plain tool-calling loop with the same tools.

### Multi-agent mode

A second architecture behind one flag (`agent.mode: single | multi`), sharing the tools,
ledger, limits and reporting of the single agent.

1. **Lead investigator:** makes a first pass, writes two or three hypotheses to the
   ledger, and writes a brief for each.
2. **Hypothesis testers:** one per hypothesis, run in parallel, each with a clean
   context and a few tool calls. Each returns a verdict with evidence references.
3. **Independent skeptic:** sees only the leading hypothesis and its cited evidence, not
   the others' reasoning, and runs one check that could disprove it. It can send the
   run back to the lead once, and can run on a different model.
4. **Lead:** ranks and reports.

Rules:

- **Equal budget.** All roles share one tool-call limit and cost cap per case, the same
  as the single agent's. The equal-budget comparison is the headline result.
- **Split by hypothesis, not by data source**, because root causes show across sources.
- **Grounding is unchanged.** Evidence must cite a tool call from the same run, whichever
  role made it.
- **The single agent stays** as the default and as the baseline for the comparison.
- **Selection for the final run** follows a rule fixed in advance: the mode with higher
  top-1 service accuracy on the tuning set; ties go to the single agent.

## 4. Model layer

- LiteLLM wrapper with temperature 0, retries, and token and cost accounting per call.
- Per-provider concurrency caps and `Retry-After` handling for rate limits.
- Every call records the model identifier the provider actually served; configured ids
  are pinned where the provider allows.
- Prompt caching where a provider supports it, with cached-token prices in the accounting.
- A global spend guard that admits calls: each call reserves its worst-case cost before
  it is sent and settles to the actual cost afterwards, so neither one large call nor
  parallel workers can pass the limit. The final run's money is held in a named
  reserve that other runs cannot spend.

### Budget configuration

The limit lives in `config/budget.yaml`: a total (default $15), a safety margin (default
$1), a per-case cap and named reserves. It can be overridden for one invocation by a
flag or environment variable. No dollar amount is written in code, the limit in force is
recorded in every run, and changing it never resets what has already been spent.

### Provider and model configuration

OpenAI, Anthropic and Google are supported. The user decides which keys and models to
provide; nothing is hard-coded.

- **Keys:** read from `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`).
  A `.env.example` lists them. `.env` is gitignored.
- **Models:** `config/models.yaml` maps a short alias to a LiteLLM model string, with an
  optional price override for models LiteLLM has no price for:

  ```yaml
  models:
    haiku:
      model: anthropic/<model-id>
    gpt-small:
      model: openai/<model-id>
    gemini-small:
      model: gemini/<model-id>
      # optional: input_cost_per_mtok, output_cost_per_mtok
  default: haiku
  ```

- **Behaviour:** a model whose provider key is missing is skipped with a notice. Scripts
  and the upload page list only usable models. A model with no known price is refused
  unless a price override is set, so the spend guard always works.

## 5. Evaluation

- **Dataset:** RCAEval RE2, Online Boutique (90 cases; metrics, logs, traces).
- **Adapter:** exports RCAEval's blind task view and passes each case folder to the agent.
  The answers file is never in a path the agent can read.
- **Split:** created before any labelled result is examined, and grouped by
  service–fault pair: RE2 has three repetitions of each of 30 pairs, so 10 pairs (30
  cases) are for tuning and 20 pairs (60 cases) are held out, with no scenario on both
  sides. Tuning results cover ten scenarios and are directional only. Held-out cases are
  seen as aggregate baseline scores only until the final run.
- **Held-out gate:** any run whose case list contains a held-out case is refused without
  an explicit final flag, whichever option or script selected it.
- **Runner:** runs cases in parallel and caches by a fingerprint of everything that can
  change the output (model settings, prompts, tools, limits, mode, source code, case
  contents, memory snapshot). It resumes unfinished cases of a frozen manifest; a case
  that still fails after one retry is scored as a miss. Memory is off by default; `--memory` turns it on, and the
  two conditions are cached separately.
- **Scoring:** RCAEval's official evaluator for headline numbers (accuracy at top 1, 3
  and 5, at service and indicator level), always given the answers subset that matches
  the cases run. Confidence intervals and paired differences are bootstrapped by
  service–fault pair.
- **Baselines, as a ladder:** BARO (metrics only); guarded BARO, which skips series that
  are flat in the normal window; multi-source BARO if it runs in the
  pinned setup (all inputs, no model); a single model call given the same summary
  statistics (a model, no loop); and the plain tool loop (a loop, no ledger or verify).
  Each rung removes one explanation for a gain.
- **Agreement with the statistic:** how often the agent's top answer equals BARO's, with
  accuracy split by agree and differ, to show the agent is not just echoing it.
- **Noise:** five tuning cases run three times each; the flip rate is quoted beside
  every comparison.
- **Onset detection:** `find_onset` is scored against RCAEval's known start times, free.
- **Manual audit:** on a seeded sample of reports, whether cited evidence supports each
  claim, and whether recommended actions pass a four-point rubric.

### Field set

Seven hand-built cases in non-RCAEval shapes, each mixing tables, logs and documents,
generated by seeded scripts and committed. Six are incidents (web server, batch job,
database, queue, a config-diff case, a victim-versus-cause case) and one is a healthy
system where the right answer is `no_fault_found`. One incident carries a planted
instruction naming the wrong component. Free-text answers are scored deterministically
by alias match. Results are reported separately as counts out of seven: the cases are
self-authored and too few for a rate, so they show that the upload path works and catch
regressions, and nothing more.

### Scripts

| Script | Purpose |
|---|---|
| `run_eval.py` | Run one configuration on a case set |
| `compare_models.py` | Run several models on the same cases and print one table: accuracy, cost per case, tokens, tool calls, wall time, paired comparison, with BARO as a free row. Defaults to the tuning subset. |
| `failure_report.py` | Per-case table: fault type, rank of the true answer, steps used, whether the root service was inspected |
| `seed_memory.py` | Fill the memory from tuning cases and their true answers |

### Planned experiments

1. BARO and multi-source BARO baselines (free); onset-detection accuracy (free).
2. Pilot on five tuning cases, to measure cost per case.
3. Noise check: five tuning cases, three repeats.
4. Agent on the tuning subset, iterating.
5. Single-call baseline on the tuning subset.
6. Field set with the selected configuration.
7. Ablation: ledger and verify on versus off, tuning subset.
8. Single agent versus multi-agent at equal budget, tuning subset.
9. Model comparison on the tuning subset.
10. Final run of the selected configuration and mode, and the single-call baseline, on
    the 60 held-out cases.
11. If budget remains: memory on versus off on the held-out cases (see section 7).
12. If budget remains: the non-selected mode on the held-out cases, as a secondary result.
13. If budget remains: RCAEval's reference LLM agent; separate ablation arms; the
    dependency tool on versus off.

## 6. Upload page

A Streamlit app that runs locally.

- **Inputs:** dropped files or a benchmark case, an optional incident time and
  description, and a model choice.
- **Live view:** tool calls and ledger changes as they happen; one lane per role in
  multi-agent mode.
- **Report:** root cause, confidence, evidence with charts of the cited metrics,
  recommended actions with risk levels, data requests, cost, and a Markdown download.
- **Add files and continue:** more files can be added to a finished run, which then
  continues with its ledger; the report shows what changed.
- **Feedback form:** was the diagnosis correct, and what fixed it. Feeds the memory.
- **Past-runs viewer:** replay any saved trajectory without calling a model.
- **Privacy notice:** uploaded data is sent to the chosen model provider.

## 7. Incident memory (optional)

Long-term store of past incidents the agent can consult. Off by default in evaluation so
headline numbers are unaffected.

- **Record written after each run:** symptom summary, affected components, diagnosed root
  cause and fault type, key evidence, recommended actions, and an outcome.
- **Outcome:** confirmed, rejected or unverified, set through the feedback form. Records
  are stored either way, so the memory does not reinforce unchecked guesses.
- **Retrieval:** `search_past_incidents` returns confirmed records as leads and rejected
  diagnoses separately as counterevidence. Unverified records appear only when nothing
  confirmed matches, marked as unchecked. A memory record is never an evidence item.
- **Use:** a hit is a lead, not an answer (see the ledger checks in section 3).
- **Storage:** a local database file with keyword search (BM25) over the symptom summary
  plus component-name matching. No embeddings for now; they can replace it later behind
  the same tool.
- **Switch:** one config flag.

### Memory experiment

Fill the memory from the 30 tuning cases with their true answers as confirmed outcomes,
then run the 60 held-out cases with memory on and off.

- Memory is frozen during the run: it is read but not written, so results do not depend
  on case order.
- Seeding is done by `seed_memory.py`; the agent never has a path to the answers file.
- Costs one extra held-out pass, so it runs only if budget remains.

## 8. Safety

- **Database access:** DuckDB is read-only with external file and network access disabled.
- **Prompt injection:** file content is treated as data and returned only inside a marked
  data field; the system prompt tells the agent to ignore instructions found in it. This
  is tested: a field-set case plants an instruction and fails if the agent follows it.
- **Deployment:** local only, no authentication.

## 9. Repo layout

```
src/incidents_agent/{ingest,tools,agent,llm,memory,eval}/
app/            # Streamlit
scripts/        # run_eval, compare_models, failure_report, eval_onset, seed_memory,
                # investigate, replay, budget, check_models
config/         # models.yaml, budget.yaml
evalsets/field/ # seven hand-built cases
.github/        # CI workflow, PR template
tests/          # ingest, tools, ledger checks, eval caching, memory
runs/           # trajectories, predictions, spend log (gitignored)
```

## 10. Code style

All Python follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html); the working rules are
summarised in `CLAUDE.md`.
`mypy` checks types in strict mode. `ruff` enforces what it can (80-character lines,
Google docstrings, import rules) and is
set up with the project skeleton.

## 11. Build order

| Step | Work | Output |
|---|---|---|
| Foundations | Setup, export RE2-OB, run BARO, ingest layer, generic tools, model layer | Baseline scores, tested tools |
| Agent and evaluation | Agent with ledger and verify, trajectory and cost logging, eval runner, iterate on the tuning subset, ablation, model comparison | Agent scores against BARO |
| Multi-agent | Lead, hypothesis testers and skeptic behind a flag; equal-budget comparison | Single versus multi result |
| Product and write-up | Upload page, incident memory, final held-out run, optional experiments, README with results and failure analysis | Demoable product and write-up |

Phase-by-phase detail is in `planning/`.

## 12. Risks

- **Little headroom:** BARO is strong at top 3 to 5 (0.98 top-5 at service level on RE1-OB
  per RCAEval's docs; 1.00 top-3 on the RE2-OB tuning cases). Its RE2-OB top-1 is
  held down by one bursty series, so the comparison also uses a guarded BARO. If the
  agent cannot beat the stronger of the two, the writeup reports that along with cost
  and failure analysis.
- **Generic tools may score lower** than tools built for RCAEval's format.
- **Upload-path accuracy is weakly evidenced:** seven self-authored cases, reported as
  such.
- **Small sample:** 90 cases in 30 scenarios; all comparisons carry intervals resampled
  by scenario.
- **Cost per case:** at the default limit the final-run allocation implies $0.05 per case, untested until a
  five-case pilot.
- **Setup evidence:** [Phase 01](planning/01-foundation-and-environment.md#5-verification-evidence)
  records the Python and RCAEval versions, dataset size, and case count.
  [Phase 03](planning/03-ingest-and-tools.md#5-dataset-notes) records the CSV headers.

### If the budget runs short

Every feature is built. Paid experiments are dropped in this order; the final held-out
run is never dropped.

1. Dependency tool on versus off; separate ablation arms
2. RCAEval's reference LLM agent baseline
3. Secondary held-out run of the non-selected mode
4. Memory experiment
5. Model comparison
