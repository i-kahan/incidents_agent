# Phase 12: Good Versus Bad Comparison

**Status:** Not started. Depends on Phase 11.

## 1. Objective
Let the user add an optional second set of files that is known to be good: a run that
worked, the previous version, another machine, a passing CI job. The agent then asks
"what differs from the good set?" instead of "what changed after the incident time?".
This covers the most common debugging question outside operations, "it worked
yesterday", where there is no incident time at all. Without a reference set, nothing
changes.

## 2. Next Steps (Checklist)

**Workstream A: Ingest**
- [ ] Two inputs: the suspect files (required) and the reference files (optional).
      Each table and document is tagged `suspect` or `reference` in the catalog.
- [ ] Files are paired across the two sets by name, then by schema. Unpaired files are
      listed in the catalog, not dropped.
- [ ] Tables from the two sets with the same schema can be queried together through a
      view with a `set` column.

**Workstream B: Tools and triage**
- [ ] `compare_windows` gains a split mode: `by_time` (today) or `by_set`.
- [ ] `search_text` reports message patterns found only in the suspect set, only in the
      reference set, and those whose rate changed.
- [ ] `time_series` can align the two sets by time since start, so two runs of
      different lengths line up.
- [ ] Document chunks paired across sets are compared by section: config keys whose
      value differs, runbook or note sections added or removed.
- [ ] Triage (Phase 11) gains a "differs from reference" section.

**Workstream C: Agent, entry points and report**
- [ ] When a reference set is present, the briefing says so and names the paired files;
      the verify step must check that the suspected difference is absent from the
      reference set.
- [ ] The report states its findings as differences from the reference.
- [ ] CLI and `why`: `--reference <folder>`. Upload page: an optional "Known good
      files" slot beside the main upload.

**Evaluation**
- [ ] A separate comparison set of hand-built cases, reported apart from the Phase 06
      field set: a config change between two versions; a failing and a passing CI log;
      a slow and a normal run of the same job; a reference set that differs only in
      noise (the right answer is "no meaningful difference").
- [ ] On RCAEval cases, a reference set can be cut from the healthy period before the
      fault. Score `by_set` against `by_time` on the tuning cases as a check that the
      two modes agree when they should.

**Tests**
- [ ] Pairing by name and by schema; unpaired files listed.
- [ ] `by_set` on a fixture where one column differs only between sets.
- [ ] Patterns found only in one set, with timestamps and ids masked, so two identical
      runs at different times show no difference.
- [ ] A run with no reference set produces the same trajectory as before this phase.
- [ ] Update `flowchart-system-overview.svg` for the second input.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: The reference set is a split mode of existing tools, not new tools

* **Decision:** Comparison by set is a parameter on `compare_windows`, `search_text`
  and `time_series`.
* **Context/Constraint:** A small tool surface keeps the agent's choices simple and
  keeps the benchmark tools and the product tools the same code.
* **Trade-off:** Some tool descriptions grow; the prompt has to explain both modes.

#### Decision 2: Logs are compared by message pattern, not line by line

* **Decision:** Log lines are reduced to patterns with variable parts (times, ids,
  numbers) masked, and patterns are compared.
* **Context/Constraint:** Two runs never produce identical lines.
* **Rejected alternatives:** A plain text diff of the two files, which reports nearly
  every line as changed.
* **Trade-off:** A fault that shows only in a variable value (one id that always fails)
  needs `run_sql` or `search_text` to find.

## 4. Constraints

* **The reference set is optional.** *Source: owner.* **Implication:** every path works
  without it, and a test checks that a run without it is unchanged.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
