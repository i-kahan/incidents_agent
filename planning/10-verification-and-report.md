# Phase 10: Verification & Report

**Status:** Complete. Final held-out, field, and optional arms published under `results/final/`; clean-clone replay, lint/test, and `REPORT.md` / `README.md` verified. (Final CI check runs on PR merge to `main`.)

## 1. Objective
Produce the final numbers once, on held-out cases, prove the whole system works from a
clean clone, and only then assemble `REPORT.md` and `README.md` by aggregating the
planning docs. Nothing is written in the report before it is true.

## 2. Next Steps (Checklist)

**Final run**
- [x] Freeze the agent configuration and mode (single or multi) chosen on the tuning
      set by Phase 07's rule; record its hash and the git commit.
- [x] Run the 60 held-out cases once with `--final`; no changes to the agent afterwards.
- [x] Score against BARO, and multi-source BARO if it ran, on the same 60 cases, each
      against the held-out answers subset, with pair-resampled intervals and paired
      differences. State the difference in inputs between each baseline and the agent.
- [x] Run the single-call baseline on the same 60 cases, from the `final` reserve, and
      include it in the paired comparison.
- [x] Report agreement with BARO's top answer on the held-out cases, with accuracy split
      by agree and differ.
- [x] Report `find_onset`'s error on the held-out cases (free).
- [x] Quote the noise estimate from Phase 06 beside every difference reported.
- [x] Record the model identifiers actually served during the final run, and check they
      match those served during tuning; a mismatch is stated in the report.
- [x] Run `failure_report.py` on the held-out results; write up the failure categories.
- [x] Run the field set once with the frozen configuration; report the counts in their
      own table with the stated limits.
- [x] If budget remains, in this order: the memory experiment (Phase 09), the
      non-selected mode on held-out cases as a secondary result, then RCAEval's
      reference agent as a second baseline.
- [x] Commit result files under `results/final/`; record total spend from the guard.
- [x] Publish with the results: the paired held-out comparison against BARO; one
      representative failure with its full trajectory; and replay instructions that
      work with no API keys, using the past-runs viewer on the committed trajectories.

**Verification**
- [x] `make lint` and `make test` clean; no skipped tests other than `live` and
      dataset-dependent ones, each explained.
- [x] Search for stray debug code, TODOs and secrets; `.env` untracked.
- [ ] CI is green on the final commit, and `main` requires it.
- [x] Fresh-clone rehearsal: follow only the written instructions on a clean checkout —
      install, configure one provider, export data, dry-run eval, start the app.
- [x] Update every phase file's status to its true final state.
- [x] Confirm every phase file has its AI-use notes.

**Write-up**
- [x] Final pass on the five SVGs so they match the built system; embed them in
      `README.md` and `REPORT.md`.
- [x] Write `REPORT.md` by aggregation (see `00` §6): how to run, what was built and
      what's left, results, trade-offs, AI tools used.
- [x] State the known limits in `REPORT.md`, taken from §10 of
      [`future-improvements.md`](future-improvements.md), and link that document as
      the roadmap.
- [x] Write `README.md`: what the project is, one diagram, the headline table,
      quickstart, the licence, and attribution for RCAEval with the papers to cite.
- [x] Read-through as a reviewer: can every decision and number be traced to
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
The free tuning-onset run detected all 30 cases, with median absolute error
0 seconds, p90 71.3 seconds, and 26/30 within 60 seconds. See
[`onset-tuning.json`](../results/phase-06/onset-tuning.json).

The existing full tuning run at `3154d2d` used single mode and served
`gpt-4o-mini-2024-07-18`. Its 24/30 service top-1 accuracy and $0.22566 cost
are tuning evidence from before the prompt and citation changes in #40. The
local comparison folder contains machine-local run paths; release artifacts
are generated separately. A new full tuning run from `41f16dd` and its single-call baseline each
covered all 30 cases. Both served `gpt-4o-mini-2024-07-18`. The primary reached
24/30 service top-1 at $0.2700693 and the baseline reached 8/30 at $0.0213642.
Scaling the measured costs from 30 to 60 cases and doubling for retries
puts the two final arms at an estimated $1.165734, within the $1.25 reserve.
The selected configuration passed one of seven synthetic field fixtures
($0.0613005). Those fixtures measure development coverage of non-benchmark
input shapes. The joint ledger/verify ablation scored 27/30. The paired
difference for the selected agent versus the ablation was −0.100
[−0.200, 0.000] with a historical top-1 flip rate of 0.133.
The three configured models completed on tuning. Service AC@1 was 24/30 for
`gpt-small`, 16/30 for `gemini-small`, and 0/30 for `haiku`. The tuning
prerequisites incurred $2.707663125. The guard recorded $6.122381850 total,
zero held, and the full $1.25 final reserve.
[Portable tuning evidence](../results/phase-06/phase-10-tuning.json).

The fresh 15-case audit is prepared. Human judgments remain outstanding.
The browser rehearsal covered the designated three cases, file continuation
and feedback controls. See [Phase 08](08-upload-app.md). The rehearsal exposed
a module lookup error in `make app`. Launching Streamlit via
`python -m streamlit` fixed it.

Local verification reported 801 passing tests, five live tests deselected,
and 93% line coverage. The publication integration used synthetic cases and
the official evaluator. A separate CLI and Streamlit replay check used a
relocated bundle after removing the original cache and provider credentials.
The active GitHub ruleset requires the `lint-and-test` check on `main`.

A fresh clone installed the locked dependencies, exported from an existing
extracted RE2-OB dataset, verified the split, regenerated the BARO baselines,
scored a three-case dry run at $0 and served the app health endpoint. The
provider readiness check found `gpt-small` usable without making a call.
The CLI replay validated one citation on a copied demo trajectory with no
credentials or original cache.
The rehearsal exposed a missing `make split` step in the quickstart, now
documented. The final published bundle will need its own clean-clone replay
after final evaluation.

### Release commands

Use the checked-in instructions in the README for installation and dataset
export. Run tuning checks and capture their saved manifests before freezing.

```sh
uv run --locked --group benchmark python -m scripts.budget
uv run --locked --group benchmark python -m scripts.audit_reports \
  --output runs/phase-10/audit-template.json prepare TUNING_RUN/reports.json
uv run --locked --group benchmark python -m scripts.audit_reports \
  --output runs/phase-10/audit-summary.json summarize \
  runs/phase-10/audit-template.json HUMAN_JUDGMENTS.json
```

Keep an evidence receipt as JSON with the keys `tuning` (the selected
run manifest), `audit` (completed audit summary), `browser` (manual browser
check results), `prerequisites` (experiment disposition and cost evidence),
and `final_estimate_usd` (combined primary and single-call estimate including
retry margin). Evidence paths are relative to the receipt. Check that the
tuning manifest matches the selected behavior, that the browser notes cover
the required three cases, and that the spend guard has no pending charges.

Once the tooling is merged and CI-green, refresh `origin/main`, check out the
same commit on local `main`, then run these commands in order:

```sh
uv run --locked --group benchmark python -m scripts.final_eval freeze \
  --evidence runs/phase-10/evidence.json
uv run --locked --group benchmark python -m scripts.final_eval run primary
uv run --locked --group benchmark python -m scripts.final_eval run single-call
uv run --locked --group benchmark python -m scripts.final_eval run field
```

After the required arms, run `memory`, `multi`, then `rcaeval-reference` when
funded. Prepare the
memory seed before freezing if it is planned. Use `omit ARM --reason REASON`
to record budget or tooling omissions. The reference arm uses the upstream
loop with the project spend guard. Use the
same `run ARM` command after interruption to resume the registered cache run.
Once optional arms are complete or omitted, publish from saved evidence
without provider credentials:

```sh
uv run --locked --group benchmark python -m scripts.final_eval publish
```

Pass `--out` with a new directory to rebuild derived artifacts for comparison.

Before staging the bundle, review the full canonical trajectories and
published metadata for credentials and machine-specific values. Verify the
bundle on a fresh clone with API keys and the original cache absent. Record
the execution commit from `release.json` and verify CI on the publication
commit.

## 6. AI-use notes (feeds `REPORT.md`)
Codex implemented the release tooling and replay source, reconciled the
available tuning evidence, and ran the free tuning-onset check. Human audit
judgments and final result interpretation are separate release work.
