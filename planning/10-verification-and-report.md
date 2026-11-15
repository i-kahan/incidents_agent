# Phase 10: Verification & Report

**Status:** Not started

## 1. Objective
Produce the final numbers once, on held-out cases, prove the whole system works from a
clean clone, and only then assemble `REPORT.md` and `README.md` by aggregating the
planning docs. Nothing is written in the report before it is true.

## 2. Next Steps (Checklist)

**Final run**
- [ ] Freeze the agent configuration and mode (single or multi) chosen on the tuning
      set by Phase 07's rule; record its hash and the git commit.
- [ ] Run the 60 held-out cases once with `--final`; no changes to the agent afterwards.
- [ ] Score against BARO, and multi-source BARO if it ran, on the same 60 cases, each
      against the held-out answers subset, with pair-resampled intervals and paired
      differences. State the difference in inputs between each baseline and the agent.
- [ ] Run the single-call baseline on the same 60 cases, from the `final` reserve, and
      include it in the paired comparison.
- [ ] Report agreement with BARO's top answer on the held-out cases, with accuracy split
      by agree and differ.
- [ ] Report `find_onset`'s error on the held-out cases (free).
- [ ] Quote the noise estimate from Phase 06 beside every difference reported.
- [ ] Record the model identifiers actually served during the final run, and check they
      match those served during tuning; a mismatch is stated in the report.
- [ ] Run `failure_report.py` on the held-out results; write up the failure categories.
- [ ] Run the field set once with the frozen configuration; report the counts in their
      own table with the stated limits.
- [ ] If budget remains, in this order: the memory experiment (Phase 09), the
      non-selected mode on held-out cases as a secondary result, then RCAEval's
      reference agent as a second baseline.
- [ ] Commit result files under `results/final/`; record total spend from the guard.
- [ ] Publish with the results: the paired held-out comparison against BARO; one
      representative failure with its full trajectory; and replay instructions that
      work with no API keys, using the past-runs viewer on the committed trajectories.

**Verification**
- [ ] `make lint` and `make test` clean; no skipped tests other than `live` and
      dataset-dependent ones, each explained.
- [ ] Search for stray debug code, TODOs and secrets; `.env` untracked.
- [ ] CI is green on the final commit, and `main` requires it.
- [ ] Fresh-clone rehearsal: follow only the written instructions on a clean checkout —
      install, configure one provider, export data, dry-run eval, start the app.
- [ ] Update every phase file's status to its true final state.
- [ ] Confirm every phase file has its AI-use notes.

**Write-up**
- [ ] Final pass on the four SVGs so they match the built system; embed them in
      `README.md` and `REPORT.md`.
- [ ] Write `REPORT.md` by aggregation (see `00` §6): how to run, what was built and
      what's left, results, trade-offs, AI tools used.
- [ ] State the known limits in `REPORT.md`, taken from §10 of
      [`future-improvements.md`](future-improvements.md), and link that document as
      the roadmap.
- [ ] Write `README.md`: what the project is, one diagram, the headline table,
      quickstart, the licence, and attribution for RCAEval with the papers to cite.
- [ ] Read-through as a reviewer: can every decision and number be traced to
      evidence in the repo? Flag anything shaky.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: `REPORT.md` by aggregation, at the end

* **Decision:** The report is assembled from the phase files in this phase, not drafted
  along the way. A skeleton with empty sections exists from the start.
* **Context/Constraint:** The phase files already hold run steps, decisions, evidence
  and open items in a form made to be lifted.
* **Trade-off:** The write-up lands late, but it cannot drift from what was built.

#### Decision 2: One held-out run, reported as it comes out

* **Decision:** The held-out set is run once with the frozen configuration and the
  result is reported whatever it is.
* **Context/Constraint:** Re-running until the number looks good turns a held-out set
  into a second tuning set. A result that does not beat BARO, explained with a failure
  analysis and cost figures, is still a credible result; a tuned one is not.
* **Trade-off:** No second chance if the final run exposes a fixable problem. Such a fix
  is listed under "what's left", not applied and re-scored.

#### Decision 3: Fresh-clone rehearsal as the release gate

* **Decision:** The instructions are executed on a clean checkout, following only what
  is written; gaps are fixed in the docs.
* **Context/Constraint:** Setup needs keys, a model file and a dataset download that a
  reader cannot guess.
* **Trade-off:** A full cold run-through for every release of the write-up.

## 4. Constraints

* **Write-up file name.** *Source: owner.* **Implication:** the final document is
  `REPORT.md`; there is no `SOLUTION.md`.
* **Every claim is traceable.** *Source: owner.* **Implication:** the read-through
  checks that each result and decision points to a committed file or phase entry.
* **Budget.** *Source: owner.* **Implication:** optional runs happen only after the
  final run is paid for and the guard shows what remains.

## 5. Verification evidence
*Final tables, spend, and the rehearsal outcome go here.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
