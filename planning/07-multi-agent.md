# Phase 7: Multi-Agent Investigation

**Status:** Not started

## 1. Objective
Add a second agent architecture — a lead investigator, parallel hypothesis testers and
an independent skeptic — behind one configuration flag, and measure on the tuning set
whether it beats the single agent **at the same budget**. Ends with both architectures
runnable through the same harness and an equal-budget comparison on disk.

See [`flowchart-multi-agent.svg`](flowchart-multi-agent.svg).

## 2. Next Steps (Checklist)

**Workstream A — Roles (`agent/roles.py`, `agent/prompts.py`)**
- [ ] **Lead investigator:** reads the catalog, makes a short first pass, writes two or
      three hypotheses to the ledger, and writes a brief for each: the hypothesis, what
      would confirm it, what would refute it.
- [ ] **Hypothesis tester:** receives one brief and a small tool budget, starts with a
      clean message history, and returns a structured verdict (supported / rejected /
      undecided, confidence, evidence references) to the ledger.
- [ ] **Independent skeptic:** receives only the leading hypothesis and its cited
      evidence — not the lead's or testers' reasoning — runs one check that could
      disprove it, and returns "holds" or "disproved" with evidence.
- [ ] One prompt per role in the versioned prompts module; hashes recorded per run.
- [ ] The skeptic's model is configurable separately, so it can differ from the lead's.
      For the single-versus-multi comparison every role uses the same model as the
      single agent; a different skeptic model is a separate, labelled configuration.

**Workstream B — Graph (`agent/multi_graph.py`)**
- [ ] Graph: orient → lead → testers (fan-out, run in parallel) → merge → skeptic →
      report, with one edge from skeptic back to lead when the hypothesis is disproved.
- [ ] Ledger writes from parallel testers merged through a reducer keyed by hypothesis
      id, so no verdict overwrites another.
- [ ] One shared budget object: the total tool-call, model-turn, wall-time and cost
      limits are the same as the single agent's, split across roles by a configurable
      allocation. Default for 15 tool calls: lead 3, testers 3 each for up to three
      hypotheses, skeptic 2, and 1 held back for the lead's correction if the skeptic
      refutes. The skeptic's and correction's share is reserved before the fan-out.
- [ ] Unused budget from a tester that finishes early returns to the shared pool.
- [ ] Each tester may update only its own hypothesis. Verdicts are merged in
      hypothesis-id order, so the result does not depend on which tester finished first.
- [ ] Partial fan-out is defined: a tester that times out, errors or runs out of budget
      leaves its hypothesis `undecided` with the reason recorded; ranking proceeds on
      the verdicts that exist, and an undecided hypothesis ranks below a supported one
      and above a rejected one.
- [ ] Limits behave as in Phase 05: on exhaustion, a forced report from the ledger.
- [ ] `agent.mode: single | multi` in the agent configuration; the mode is part of the
      run-cache key.

**Workstream C — Run record and evaluation**
- [ ] Every model and tool call in the trajectory is tagged with the role and, for
      testers, the hypothesis id.
- [ ] The grounding check accepts evidence from any role's tool calls in the same run,
      and nothing from outside it.
- [ ] `--mode` option on `run_eval.py`; `compare_models.py` gains a mode column so
      single and multi appear side by side.
- [ ] Per-role breakdown in `failure_report.py`: tokens and tool calls by role, how
      often the skeptic overturned the lead, and whether it was right to.

**Experiment (paid)**
- [ ] Single versus multi on the 30 tuning cases: same model for every role, same
      candidate lists and time windows, same total limits. Report accuracy with
      intervals, the paired difference with its interval, and realised cost, tokens and
      wall time per case beside the equal caps.
- [ ] Record the selection rule's outcome (Decision 4) in §5.

**Tests**
- [ ] Unit tests: brief and verdict schemas, budget allocation and pool return, the
      ledger reducer under concurrent verdicts.
- [ ] Graph tests with the scripted fake client: happy path with three testers; a tester
      that hits its limit; a tester whose tool errors; skeptic disproves and the run
      returns to the lead exactly once; shared budget exhausted mid-fan-out.
- [ ] Test that the skeptic's input contains no message from the lead or the testers.
- [ ] Test that total tool calls in multi mode never exceed the single-agent limit.
- [ ] One `live` test: a single tuning case in multi mode on the cheapest model.
- [ ] Keep `flowchart-multi-agent.svg` in sync with the graph as built.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Split by hypothesis, not by data source

* **Decision:** Sub-agents are assigned one hypothesis each. They are not specialised
  by table or telemetry type.
* **Context/Constraint:** A root cause usually shows across sources: a metric shift, an
  error in the logs and a slow span belong to one story. A tester following one
  hypothesis can cross sources freely.
* **Trade-off:** Each tester rediscovers some structure the others also look at, so
  there is duplicated tool use.
* **Rejected alternatives:** one specialist per source (metrics, logs, traces) with a
  coordinator merging findings. It keeps contexts small, but the merge step is exactly
  where cross-source reasoning has to happen, with the least information. It also
  assumes RCAEval's three modalities, which an arbitrary upload does not have.

#### Decision 2: The skeptic sees the claim and the evidence, not the reasoning

* **Decision:** The skeptic starts from a fresh context containing the leading
  hypothesis, its cited tool results and the catalog.
* **Context/Constraint:** An agent asked to check its own conclusion tends to defend
  it. Withholding the argument forces the skeptic to judge the evidence on its own.
* **Trade-off:** The skeptic may repeat a check already done. It also adds a
  conversation per case that must be paid for out of the shared budget.

#### Decision 3: Equal budget is the comparison that counts

* **Decision:** Multi mode gets the same total tool-call limit and cost cap per case as
  single mode. The comparison at equal budget is the headline; any run with a larger
  budget is labelled as such.
* **Context/Constraint:** A multi-agent system that scores higher while spending three
  times as much has shown that spending helps, not that the structure does.
* **Trade-off:** Each role works with a small allowance, which may understate what the
  architecture could do with more. That is reported as a limitation, not fixed by
  quietly raising the limit.

#### Decision 4: The architecture for the final run is chosen by a rule set in advance

* **Decision:** Multi mode goes to the held-out run only if the lower bound of the 95%
  paired bootstrap interval (resampled by service–fault pair) for its gain in top-1
  service accuracy on the tuning set is above zero. Otherwise single mode is chosen. If
  budget remains after the final run, the other mode is also run on held-out cases and
  reported as secondary.
* **Context/Constraint:** Choosing after seeing held-out numbers would turn the held-out
  set into a tuning set, and "a difference inside the interval" can be read more than
  one way. The single agent is the default because it is simpler and cheaper, so the
  burden of proof is on the more complex design.
* **Trade-off:** With ten scenarios in the tuning set the rule has little power: multi
  mode could be better and still not clear the bar. In that case it appears only in the
  tuning comparison and, budget permitting, the secondary held-out run. The report
  states this, so "not selected" is not read as "shown to be no better".

#### Decision 5: The single agent stays

* **Decision:** Multi mode is an addition behind a flag. The single agent from Phase 05
  is kept, tested and remains the default.
* **Context/Constraint:** It is the baseline that gives the comparison meaning, and the
  simpler system to fall back on.
* **Trade-off:** Two graphs to maintain. Tools, ledger, limits and reporting are shared
  so the duplicated surface is the graph wiring and the prompts.

## 4. Constraints

* **Both multi-agent and incident memory are in scope.** *Source: owner.*
  **Implication:** memory must work in both modes; in multi mode only the lead may call
  `search_past_incidents`, and memory-origin hypotheses follow the same ledger rule.
* **The configured dollar limit ($15 by default).** *Source: owner.* **Implication:** the comparison runs on the tuning set
  only, with one model; its allocation is in Phase 06, Decision 6.
* **Phase 05 builds for this.** *Source: this plan.* **Implication:** the investigate
  step in Phase 05 is written as a function of (task, tool budget, ledger handle,
  message history) so a tester is the same code with a different brief.

## 5. Verification evidence
*The equal-budget comparison table and the selection outcome go here.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
