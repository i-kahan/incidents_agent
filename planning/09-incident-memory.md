# Phase 9: Incident Memory

**Status:** Wave 4 complete (store, BM25 retrieval, `search_past_incidents` tool, frozen mode, and ledger/graph integration tested offline; app feedback wiring, multi-mode lead-only integration, and `seed_memory.py` / experiment remain for Wave 5).

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

**Experiment (only if budget remains)**
- [ ] `scripts/seed_memory.py`: fills a fresh memory from the 30 tuning cases, using
      their true answers as confirmed outcomes. The resulting snapshot is frozen and
      its hash recorded; the hash is part of the run-cache key.
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
- [ ] Test that `seed_memory.py` writes only tuning-case ids.
- [x] Graph test with the fake client: a memory-origin hypothesis with no current
      evidence cannot be marked supported.
- [x] Update `flowchart-system-overview.svg` if the memory wiring changed. *(Verified existing diagram already matches Wave 4 wiring.)*

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
*Experiment table and tool-usage counts go here, if the experiment runs.*

## 6. AI-use notes (feeds `REPORT.md`)
- Wave 4 (`feature/phase-09a-memory`): implemented `incidents_agent.memory.store` (`MemoryStore` over SQLite with `BEGIN IMMEDIATE` writes, `FrozenMemoryError`, `MemoryRecord`, `snapshot_hash()`, Okapi BM25 + component-boost search, and `search_past_incidents`; `build_record_from_run` later moved to `agent/memory_record.py`), wired `search_past_incidents` into `ToolRegistry` behind `AgentConfig.memory`, enforced memory-evidence isolation in `HypothesisLedger` and verification checks, wired end-of-run `unverified` persistence in `run_investigation` and `continue_run`, added `--memory` / `--memory-path` to `scripts/investigate.py`, and added unit and integration tests in `tests/memory/test_store.py`.
- Note: `memory/store.py` avoids importing `incidents_agent.tools` because `tools/__init__.py` imports `registry` eagerly; `registry.py` registers `search_past_incidents` directly against `memory/store.py`.
- Wave 5 (`feature/phase-08b-lanes-and-feedback`): wired the Streamlit upload app (`app/main.py` and `app/runtime.py`) to `MemoryStore` for feedback outcome/fix updates, record deletion, memory status display, and continuation feedback carryover without modifying `src/incidents_agent/`.
