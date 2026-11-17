# Phase 9: Incident Memory

**Status:** Wave 4 and Wave 5 offline implementation complete (seeding, frozen
evaluation, and Phase 07 multi-mode integration). App feedback landed in Phase 08b;
the paid held-out memory experiment remains deferred.

## 1. Objective
Give the agent a long-term record of past incidents it can consult for leads, without
letting it reinforce its own unchecked guesses, and measure on held-out cases whether
that record helps. Ends with a memory that is off by default, a feedback loop from the
app, and (budget permitting) an on-versus-off result.

## 2. Next Steps (Checklist)

**Feature**
- [x] Store (`memory/store.py`): a local database file with one record per incident —
      symptom summary, affected components, diagnosed root cause, fault type, key
      evidence, recommended actions, outcome (confirmed / rejected / unverified), what
      fixed it, and timestamps.
- [x] Record written at the end of every run when memory is on, as `unverified`.
- [x] Retrieval: BM25 over the symptom summary plus a boost for matching component names.
- [x] `search_past_incidents` tool, returning two separate lists. **Leads:** confirmed
      records only, with root cause and fix. **Counterevidence:** rejected diagnoses for
      similar symptoms, presented as "this was tried and was wrong". Unverified records
      are returned only when there is no confirmed match, in a third list marked as
      unchecked.
- [x] Only observations from the current run can support a hypothesis. A memory record
      can suggest one or count against one; it is never an evidence item.
- [x] One configuration flag turns memory on; with it off, the tool is not offered.
- [x] A read-only (frozen) mode: lookups allowed, writes refused.
- [x] App feedback form updates the record's outcome and fix. *(Wired in Wave 5 via
      `app/runtime.py:save_feedback`, `load_feedback`, and `delete_run` against
      `MemoryStore`.)*
- [x] Ledger integration from Phase 05 confirmed: memory-origin hypotheses need current
      evidence before they can be supported.

**Seeding and frozen evaluation (offline)**
- [x] `scripts/seed_memory.py`: fills a fresh memory from the 30 tuning cases, using
      their true answers as confirmed outcomes. The resulting snapshot is frozen and
      its hash recorded; the hash is part of the run-cache key.
- [x] Evaluation requires an explicit validated seed bundle, takes a private
      read-only copy, and rejects overlap between seeded and evaluated case IDs.
- [x] Shared loop hides and refuses memory search for tester/skeptic roles, including
      attempts to read another role's memory result through `read_result`.
- [x] Rebase on merged Phase 07, wire the real multi graph and continuation, and test
      role contexts, hypothesis merging, shared limits and one-record persistence.
      *(Rebased onto merged Phase 07a/07b/07c and 08b; `roles.role_tools`,
      `investigate.investigate`, `roles.skeptic_opening`, `multi_graph`,
      `continuation`, `service`, and `comparison` enforce lead-only memory access,
      context isolation, `origin='memory'` preservation, and single-record
      persistence.)*

**Experiment (only if budget remains)**
- [ ] Run the 60 held-out cases with memory frozen and on; compare with the memory-off
      final run from Phase 10 using the paired tests from Phase 06.
- [ ] State the scope with the result: the seed holds three repetitions of each of ten
      scenarios, and the held-out scenarios are different service–fault pairs, so the
      experiment measures transfer from related incidents, not recall of known ones.
      Any benefit is claimed for this seeded snapshot only.
- [ ] Record how often the tool was called, how often its top hit matched the true root
      cause, and how often the agent followed a wrong hit.

**Tests**
- [x] Unit tests: record round trip, ranking on a small hand-built set, outcome updates,
      frozen mode refuses writes, tool absent when the flag is off.
- [x] Test that `seed_memory.py` writes only tuning-case ids.
- [x] Graph test with the fake client: a memory-origin hypothesis with no current
      evidence cannot be marked supported.
- [x] Update `flowchart-system-overview.svg` and `flowchart-multi-agent.svg` if the
      memory wiring changed. *(Diagrams include the tuning seeder, frozen evaluation
      copy, and lead-only memory access in multi mode.)*

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Keyword retrieval, no embeddings

* **Decision:** BM25 plus component-name matching, behind a tool interface that an
  embedding search could replace later.
* **Context/Constraint:** Incident text is dense with exact terms (service names, error
  strings). Embeddings would add a provider, a cost and a dependency for unclear gain at
  this scale.
* **Trade-off:** Paraphrased symptoms with no shared terms will not match.

#### Decision 2: Confirmed records are leads; rejected ones are counterevidence

* **Decision:** Every record carries an outcome set only by human feedback (or by the
  seeding script from true labels). The tool returns confirmed records as leads and
  rejected ones in a separate list as counterevidence. Unverified records are shown
  only when nothing confirmed matches, and are marked as unchecked.
* **Context/Constraint:** A memory that stores the agent's own conclusions as fact
  teaches it to repeat its mistakes. Labelling is not enough: a rejected or unchecked
  diagnosis listed beside confirmed fixes still anchors the reader. A known-wrong past
  diagnosis is useful, but as a warning.
* **Trade-off:** Until someone gives feedback, most records are unverified and rarely
  shown, so the memory is of little use early on.

#### Decision 3: Memory is a lead, enforced by the ledger

* **Decision:** A hypothesis taken from memory is tagged as such and cannot be marked
  supported without evidence from the current data.
* **Context/Constraint:** The danger of retrieval is anchoring: a similar-looking past
  incident with a different cause.
* **Trade-off:** Memory can save investigation steps but never skip verification.

#### Decision 4: Frozen memory in the experiment

* **Decision:** During the held-out run the memory is read-only.
* **Context/Constraint:** Cases run in parallel. If each run wrote to memory, later
  cases would see earlier held-out diagnoses, and results would depend on run order.
* **Trade-off:** The experiment measures a fixed memory, not one that grows in use.

#### Decision 5: Off by default in evaluation

* **Decision:** The runner defaults memory to off; `--memory` turns it on and the two
  conditions are cached separately.
* **Context/Constraint:** Headline numbers must be comparable with baselines that have
  no memory.
* **Trade-off:** None for the headline; the experiment is a separate, labelled result.

#### Decision 6: A continuation keeps human feedback; undiagnosed runs are not stored

* **Decision:** When a continuation re-saves a run's record, `created_at` and the
  fix note carry over. A `confirmed` or `rejected` outcome carries over only while the
  diagnosed root cause is unchanged; a changed diagnosis returns to `unverified`.
  A run that ranked no cause is not written to memory.
* **Context/Constraint:** Adding files is the normal way to firm up a diagnosis, and it
  must not wipe what a person already confirmed. A confirmation of one cause says
  nothing about a different one. A run that failed before ranking has nothing to lead
  from, and would only add noise to retrieval.
* **Trade-off:** A person must re-confirm after the diagnosis changes.

#### Decision 7: Memory depends on nothing above it

* **Decision:** `memory/` imports no other project package. `build_record_from_run`,
  which reads the agent's report and result types, lives in
  `agent/memory_record.py`. `tests/test_layering.py` fails if a lower package imports
  a higher one (`llm` < `ingest` < `memory` < `tools` < `agent` < `eval`).
* **Context/Constraint:** `tools` registers the memory search tool and `agent` calls
  `tools`, so a `memory` import of `agent` closed a package-level loop that only
  worked because it touched leaf modules.
* **Trade-off:** One more small module in `agent/`.

#### Decision 8: Deterministic telemetry seeds, with no inferred repairs

* **Decision:** Share the single-call baseline's bounded, deterministic metric and
  log summarizer. Use only its observed telemetry for symptoms and evidence; tuning
  labels set the confirmed service and fault. Use injection times for timestamps.
  Leave fixes and recommended actions empty.
* **Trade-off:** The seed is reproducible and costs no model calls, but has no human
  repair notes or model-authored incident narrative.

#### Decision 9: A validated bundle and private evaluation copy

* **Decision:** Publish a new directory atomically, containing SQLite plus a manifest
  with the split hash, tuning-answer hash, per-case input hashes, summary-policy
  version and logical record hash. Refuse overwrite. Validate exact tuning-only
  membership and confirmed outcomes, then use SQLite backup to capture a private
  read-only database for the evaluation. Cache by content, never source path.
* **Trade-off:** Evaluation requires a seeded bundle rather than an arbitrary app
  memory database. The manifest records provenance, not a cryptographic attestation
  of labels; evaluation does not reopen tuning answers. Seeded cases cannot be
  evaluated against their own confirmed answers. Original-file edits after capture
  do not affect workers. Recovered attempts must match the snapshot identity.

#### Decision 10: Lead-only memory in multi mode, enforced in dispatch and context

* **Decision:** In multi mode, `search_past_incidents` is offered only to the `lead`
  (including `lead_report` / correction passes). `roles.role_tools` excludes
  `MEMORY_TOOLS` for testers and the skeptic, `investigate.investigate` strips
  `MEMORY_TOOLS` for any role outside `('agent', 'lead')` and rejects forged tool
  calls or `read_result` lookups of memory results, and `roles.skeptic_opening`
  excludes any memory-stored result.
* **Context/Constraint:** Testers and the skeptic must judge the hypothesis and
  current-run data independently without anchoring on past incident narratives or
  fixes.
* **Trade-off:** Testers cannot consult memory for secondary leads; any past-incident
  context the lead wants tested must be expressed in the hypothesis statement and
  brief criteria, and still requires current-run observations to be supported.

## 4. Constraints

* **Seeding uses tuning answers only.** *Source: agreed experiment design.*
  **Implication:** the agent still never has a path to the answers file; only the
  seeding script reads it.
* **One extra held-out pass.** *Source: budget.* **Implication:** the experiment runs
  only if its allocation survives the earlier paid runs.
* **Works in both agent modes.** *Source: Phase 07.* **Implication:** in multi mode only
  the lead calls `search_past_incidents`; tests cover both modes. Phase 07a gives each
  role a tool allowlist (`investigate.Brief.tools`), so the rule is enforced by the
  loop, not by the prompt.

## 5. Verification evidence

Wave 5 offline verification after Phase 07 rebase and multi-mode integration
(2026-11-17):

- `make test` with coverage: **765 passed**, 3 existing optional-export skips,
  5 live tests deselected; **94%** total line coverage (7,909 statements, 508
  missed).
- `make lint`: Ruff check, Ruff format check (197 files), and strict mypy
  (158 source files) pass.
- Prior pre-rebase tuning-only seeding smoke (`cf42a0a`): **30 confirmed records**,
  logical SHA-256 `0c733c56fc1289dff50e781440a12370b410cd3fe0673f6ce28ada16a5d1dfeb`.
  No model calls or held-out evaluation; $0.00 task spend.

Tests cover deterministic seeding, tuning-only reads, invalid inputs and atomic
publication, snapshot isolation, content-based cache identity across memory and
modes, model comparison with frozen memory, attempt recovery, resume mismatch,
role lookup and `read_result` restrictions, multi-mode lead lookup, forged tester
and skeptic calls, memory-only support rejection, current-run support,
correction-pass memory access, continuation restriction retention, shared limits,
and single-record / frozen persistence. No paid experiment is claimed.

The Phase 10 rehearsal prepared a local frozen memory seed from the 30 tuning
cases. Its manifest records snapshot hash
`0c733c56fc1289dff50e781440a12370b410cd3fe0673f6ce28ada16a5d1dfeb`.
The held-out memory experiment remains pending.

## 6. AI-use notes (feeds `REPORT.md`)
- Wave 4 (`feature/phase-09a-memory`): implemented `incidents_agent.memory.store` (`MemoryStore` over SQLite with `BEGIN IMMEDIATE` writes, `FrozenMemoryError`, `MemoryRecord`, `snapshot_hash()`, Okapi BM25 + component-boost search, and `search_past_incidents`; `build_record_from_run` later moved to `agent/memory_record.py`), wired `search_past_incidents` into `ToolRegistry` behind `AgentConfig.memory`, enforced memory-evidence isolation in `HypothesisLedger` and verification checks, wired end-of-run `unverified` persistence in `run_investigation` and `continue_run`, added `--memory` / `--memory-path` to `scripts/investigate.py`, and added unit and integration tests in `tests/memory/test_store.py`.
- Note: `memory/store.py` avoids importing `incidents_agent.tools` because `tools/__init__.py` imports `registry` eagerly; `registry.py` registers `search_past_incidents` directly against `memory/store.py`.
- Wave 5 (`feature/phase-08b-lanes-and-feedback`): wired the Streamlit upload app (`app/main.py` and `app/runtime.py`) to `MemoryStore` for feedback outcome/fix updates, record deletion, memory status display, and continuation feedback carryover without modifying `src/incidents_agent/`.
- Wave 5 (`codex/phase-09b-memory-multi-seeding`): added offline tuning seeding,
  manifest validation and private SQLite capture; extracted the deterministic
  telemetry summarizer without changing the single-call baseline; added explicit
  frozen-memory evaluation, cache and recovery identity, and role checks in the
  shared loop. Rebased onto merged Phase 07 (`origin/main`), completed multi-graph
  and comparison integration (`roles.skeptic_opening` defense-in-depth,
  `agent_adapter.Executor` snapshot lifetime, multi-mode role/grounding/continuation
  tests, and `flowchart-multi-agent.svg`). All validation uses offline inputs and
  fake clients; experiment metrics remain deferred.
