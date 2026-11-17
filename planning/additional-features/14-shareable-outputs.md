# Phase 14: Shareable Outputs

**Status:** Not started. Depends on Phase 11; Workstream A uses Phase 13's masking.

## 1. Objective
Turn a finished investigation into things a person can hand to someone else or check
for themselves. Each output is optional and built from the saved trajectory without a
model call: a minimal evidence bundle, a notebook that explains the result, a map of
what was and was not examined, and, when source code is in the input, links from log
lines to the code that wrote them.

## 2. Next Steps (Checklist)

**Workstream A: Minimal evidence bundle**
- [ ] From a finished run, a small folder or zip that holds: the report; every cited row
      or line with a few lines of context; the triage summary; the task; the ledger; and
      a short README.
- [ ] Redacted with the Phase 13 masking unless the user turns it off; the README says
      what was masked.
- [ ] A size target (default 1 MB) and a note of what was left out to meet it.
- [ ] The bundle is itself a valid input: `why <bundle>` ingests it, and every citation
      in the report resolves inside it.

**Workstream B: Explanation notebook (optional)**
- [ ] A Jupyter notebook generated from the trajectory that explains the result: the
      question, the timeline, each hypothesis with the queries that supported or
      rejected it, charts of the cited metrics, and the conclusion.
- [ ] The query cells run against the run's local database, so a reader can change a
      query and see what happens.
- [ ] Prose comes from the report and the ledger; no model call.

**Workstream C: Coverage map (optional)**
- [ ] For each file and table: whether the agent looked at it, which time ranges and
      columns its queries covered, and which sources it never opened.
- [ ] Shown beside the conclusion in the report and the app, so "nothing found in the
      database logs" can be read as "the database logs were never examined" when that
      is the case.
- [ ] Computed from tool arguments and results in the trajectory; tools record the
      tables, columns and time bounds they touched.

**Workstream D: Link to code (optional)**
- [ ] When the input includes source files, or a repository path is given, log message
      patterns are matched to the string literals and format strings that produce them,
      giving file and line.
- [ ] When the source is a git repository, list the commits that touched those lines,
      and the commits made shortly before the incident time.
- [ ] Offered to the agent as a `find_log_source` tool; registered only when source is
      present.

**Tests**
- [ ] Bundle: every report citation resolves in the bundle; re-ingesting the bundle
      reproduces the cited values; the size cap holds.
- [ ] Notebook: generated from a fake-client run and executed top to bottom in the test.
- [ ] Coverage: a fixture run that skips one source reports it as never opened.
- [ ] Link to code: a fixture repository with known log calls; patterns map to the
      right lines, including format strings with placeholders.
- [ ] Update `flowchart-system-overview.svg` for the new outputs.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Outputs are built from the trajectory, without a model

* **Decision:** Every output in this phase is a deterministic rendering of the saved
  trajectory and database.
* **Context/Constraint:** Outputs that are shared must say exactly what the run found,
  and must cost nothing to regenerate.
* **Trade-off:** The notebook's prose is plainer than a model would write.

#### Decision 2: The evidence bundle must be a valid input

* **Decision:** A bundle can be fed back to the tool, and its citations must resolve.
* **Context/Constraint:** A bundle that cannot be checked is just a screenshot.
* **Trade-off:** The bundle keeps original file formats and context lines, so it is
  larger than a plain excerpt.

## 4. Constraints

* **Each output is optional.** *Source: owner.* **Implication:** none is produced unless
  asked for, and none is needed by another phase.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
