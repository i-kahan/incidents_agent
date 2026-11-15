# Phase 8: Upload App

**Status:** Not started

## 1. Objective
A local web page where someone drops incident files, watches the agent investigate, and
reads a report they can act on. Ends with a demo that works on a benchmark case and on a
folder of files the agent has never seen.

## 2. Next Steps (Checklist)
- [ ] Streamlit app (`app/main.py`) started by `make app`.
- [ ] Inputs: file upload (multiple), or a benchmark case picker; optional incident time
      and free-text description; model choice limited to usable configured models;
      agent mode (single or multi).
- [ ] Privacy notice before the first run: uploaded content is sent to the chosen
      model provider; secrets are masked by pattern only; and excerpts of the files
      remain in the saved trajectory and, if memory is on, in the incident memory after
      the upload itself is deleted.
- [ ] A delete control per run that removes its trajectory and any memory record made
      from it.
- [ ] Uploads written to a per-session temporary folder and removed afterwards.
- [ ] Ingest summary: tables found, rows, time ranges; documents found, with type and
      outline; how each file was classified, with a control to override a wrong
      classification and re-ingest; any skipped files with reasons.
- [ ] Time mapping shown per table (column, inferred unit, confidence). Tables with a
      low-confidence or missing mapping are highlighted, and the user confirms or
      corrects them before time-based analysis runs.
- [ ] Live view: tool calls and ledger changes streamed as the agent runs; running cost.
      In multi mode, one lane per role so parallel testers are readable.
- [ ] Report view: root cause, fault type, confidence, evidence items each linked to the
      tool call behind them (document evidence shows the cited chunk with its heading), recommended actions, total cost and time.
- [ ] Charts of the metrics cited as evidence.
- [ ] Past-runs viewer: pick any saved trajectory under `runs/` and replay its steps,
      ledger and report without calling a model.
- [ ] Markdown download of the report.
- [ ] Clear states for: no usable model, budget exhausted, inconclusive result, run error.
- [ ] Feedback form on the report (was the diagnosis correct; what fixed it) — wired to
      memory in Phase 09; stored locally until then.
- [ ] Report rendering lives in a plain function outside Streamlit, with unit tests.
- [ ] Integration test: the app's run function end to end on the synthetic fixture
      folder with the fake client.
- [ ] Manual check in a browser: one benchmark case and two field-set cases, one of
      them the config-diff case.
- [ ] Update `flowchart-system-overview.svg` if the entry points changed.

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

## 4. Constraints

* **End product is a drop-files platform.** *Source: owner.* **Implication:** the
  non-benchmark path is tested with the field set from Phase 06.
* **No ground truth on uploads.** *Source: the nature of the input.* **Implication:**
  the page presents the result as a reasoned diagnosis with evidence, not a verified one.
* **Incident time is optional.** *Source: the nature of the input.* **Implication:**
  without it the agent estimates the onset with `find_onset`; the page shows the
  estimate and lets the user correct it and re-run.

## 5. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
