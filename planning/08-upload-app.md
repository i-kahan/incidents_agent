# Phase 8: Upload App

**Status:** Wave 4 (single mode, add-files-and-continue, and past-runs viewer) built and verified offline ($0.00 spend). Wave 5 (multi-agent role lanes from Phase 07, Phase 09 memory wiring) and interactive browser click-through remain pending.

## 1. Objective
A local web page where someone drops incident files, watches the agent investigate, and
reads a report they can act on. Ends with a demo that works on a benchmark case and on a
folder of files the agent has never seen.

## 2. Next Steps (Checklist)
- [x] Streamlit app (`app/main.py`) started by `make app`.
- [x] Inputs: file upload (multiple), or a benchmark case picker; optional incident time
      and free-text description; model choice limited to usable configured models;
      agent mode (single or multi). *(Benchmark picker lists tuning cases via
      `eval.selection.resolve` and field cases; `multi` mode stays disabled until
      Phase 07 lands in Wave 5.)*
- [x] Privacy notice before the first run: uploaded content is sent to the chosen
      model provider; secrets are masked by pattern only; and excerpts of the files
      remain in the saved trajectory and, if memory is on, in the incident memory after
      the upload itself is deleted.
- [x] A delete control per run that removes its trajectory and any memory record made
      from it. *(Exposes `memory_deleter` callback for Phase 09 wiring in Wave 5.)*
- [x] Uploads written to a per-session temporary folder and removed afterwards.
- [x] Ingest summary: tables found, rows, time ranges; documents found, with type and
      outline; how each file was classified, with a control to override a wrong
      classification and re-ingest; any skipped files with reasons.
- [x] Time mapping shown per table (column, inferred unit, confidence). Tables with a
      low-confidence or missing mapping are highlighted, and the user confirms or
      corrects them before time-based analysis runs.
- [x] Live view: tool calls and ledger changes streamed as the agent runs; running cost.
      In multi mode, one lane per role so parallel testers are readable. *(Wave 4 renders
      single `agent` role lane via `view.build_live_view`; Wave 5 adds parallel multi
      lanes once Phase 07 lands.)*
- [x] Report view: root cause, fault type, confidence, evidence items each linked to the
      tool call behind them (document evidence shows the cited chunk with its heading), recommended actions, total cost and time.
- [x] Recommended actions shown with their risk level and how to verify each, read-only
      checks first.
- [x] Data requests shown when the diagnosis is provisional or inconclusive, each with
      the hypothesis it would settle.
- [x] "Add files and continue": more files can be added to a finished run. The page
      shows what the new files added to the catalog and the estimated cost, then
      continues the same run with its ledger; the report shows what changed between
      the first answer and the continued one.
- [x] Charts of the metrics cited as evidence.
- [x] Past-runs viewer: pick any saved trajectory under `runs/` and replay its steps,
      ledger and report without calling a model.
- [x] Markdown download of the report.
- [x] Clear states for: no usable model, budget exhausted, inconclusive result, no fault
      found, run error.
- [x] Feedback form on the report (was the diagnosis correct; what fixed it) — wired to
      memory in Phase 09; stored locally until then. *(Persisted atomically to
      `run_dir / 'feedback.json'` in Wave 4; Wave 5 wires to `memory/store.py`.)*
- [x] Report rendering lives in a plain function outside Streamlit, with unit tests.
- [x] Integration test: the app's run function end to end on the synthetic fixture
      folder with the fake client.
- [x] Integration test with the fake client: a run that ends with data requests is
      continued after a file is added, and the second report reflects the new table.
- [ ] Manual check in a browser: one benchmark case and two field-set cases, one of
      them the config-diff case. *(Headless `AppTest` rendering, localhost HTTP 200
      startup, and `$0.00` `RuleClient` end-to-end checks passed on `re2-ob-0008`,
      `config_diff`, and `victim_vs_cause`; interactive browser check remains for the
      owner.)*
- [x] Update `flowchart-system-overview.svg` if the entry points changed. *(Verified
      `flowchart-system-overview.svg` already reflects `Upload page (Streamlit · local)`
      -> `Ingest` -> `Agent` -> `Incident report`; no entry-point wiring changed.)*

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Streamlit

* **Decision:** The page is a single Streamlit app.
* **Context/Constraint:** The UI is a window onto the agent, not the product's core.
  Streamlit gives file upload, streaming updates and charts with no front-end build.
* **Trade-off:** Limited control over layout and no real multi-user story; fine for a
  local demo, and the report says it is not a deployment.

#### Decision 2: The app is a thin shell over the same run function the evals use

* **Decision:** The page calls the agent through the same entry point as the runner and
  renders the trajectory it produces; no agent logic lives in `app/`.
* **Context/Constraint:** The benchmark numbers are only evidence about the product if
  the product runs the same code.
* **Trade-off:** The UI can show only what the trajectory records, so anything worth
  displaying must be logged by the agent first.

#### Decision 3: Local only, no authentication

* **Decision:** The app binds to localhost and has no accounts.
* **Context/Constraint:** Incident files can contain sensitive data; hosting them
  publicly would need access control and retention rules that are out of scope.
* **Trade-off:** It cannot be shared as a link; the demo is run live or recorded.

#### Decision 4: Pure `app/view.py` and `app/runtime.py` outside Streamlit

* **Decision:** Keep all view models, citation/chunk extraction, chart series queries,
  staging, ingest overrides, continuation previews, feedback storage, and trajectory
  replay in plain Python modules (`app/view.py`, `app/runtime.py`) with no
  `streamlit` import, and avoid modifying `src/incidents_agent/`.
* **Context/Constraint:** Wave 4 streams run in parallel against frozen Phase 05
  contracts and evaluation cache fingerprints (`scripts/run_eval.py:source_fingerprint`).
* **Trade-off:** `classification_overrides` and `TimeOverride` are applied in `app/`
  (scoping `IngestConfig` and materialising `unix_time` on staged copies) rather than
  changing core agent signatures; Wave 5 can promote overrides directly if desired.

## 4. Constraints

* **End product is a drop-files platform.** *Source: owner.* **Implication:** the
  non-benchmark path is tested with the field set from Phase 06.
* **No ground truth on uploads.** *Source: the nature of the input.* **Implication:**
  the page presents the result as a reasoned diagnosis with evidence, not a verified one.
* **Incident time is optional.** *Source: the nature of the input.* **Implication:**
  without it the agent estimates the onset with `find_onset`; the page shows the
  estimate and lets the user correct it and re-run.

## 5. AI-use notes (feeds `REPORT.md`)
- Wave 4 (`feature/phase-08a-upload-app` worktree): implemented `app/view.py`,
  `app/runtime.py`, `app/main.py`, the `make app` target in `Makefile`, and
  `tests/app/{test_view.py,test_runtime.py}`.
- `app/runtime.py` resolves `SHARED_ROOT` via `git rev-parse --git-common-dir` so
  worktrees share `.env`, `runs/spend.sqlite3`, `runs/`, and `agent_tasks/re2-ob`
  with the main checkout while keeping `config/`, `splits/`, and `evalsets/field/`
  worktree-local.
- Verified at $0.00 LLM spend: `make lint` (`ruff check`, `ruff format --check`,
  strict `mypy`) clean across 126 files; `make test` green; 29 new tests in
  `tests/app/` passing (85% coverage on `app/runtime.py`, 85% on `app/view.py`);
  headless `AppTest` + localhost HTTP 200 startup verified; end-to-end `RuleClient`
  smoke checks verified on `re2-ob-0008`, `config_diff`, and `victim_vs_cause`.
