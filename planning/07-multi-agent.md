# Phase 7: Multi-Agent Investigation

**Status:** Complete. Built in 07a–07c and #32; the equal-budget comparison is in §5.
Single mode is selected for the final run by the Decision 4 rule.

## 1. Objective
Add a second agent architecture — a lead investigator, parallel hypothesis testers and
an independent skeptic — behind one configuration flag, and measure on the tuning set
whether it beats the single agent **at the same budget**. Ends with both architectures
runnable through the same harness and an equal-budget comparison on disk.

See [`flowchart-multi-agent.svg`](flowchart-multi-agent.svg).

## 2. Next Steps (Checklist)

**Workstream A — Roles (`agent/roles.py`, `agent/prompts.py`)**
- [x] **Lead investigator:** reads the catalog, makes a short first pass, writes two or
      three hypotheses to the ledger, and writes a brief for each: the hypothesis, what
      would confirm it, what would refute it.
- [x] **Hypothesis tester:** receives one brief and a small tool budget, starts with a
      clean message history, and returns a structured verdict (supported / rejected /
      undecided, confidence, evidence references) to the ledger.
- [x] **Independent skeptic:** receives only the leading hypothesis and its cited
      evidence — not the lead's or testers' reasoning — runs one check that could
      disprove it, and returns "holds" or "disproved" with evidence.
- [x] One prompt per role in the versioned prompts module; hashes recorded per run.
- [x] The context policy applies per role, since each role has its own conversation.
      `dependency_graph` is available to every role. Only the lead writes data
      requests and recommended actions, from the merged ledger.
- [x] The skeptic's model is configurable separately, so it can differ from the lead's.
      For the single-versus-multi comparison every role uses the same model as the
      single agent; a different skeptic model is a separate, labelled configuration.

**07a — Shared-run contracts (prerequisite for B, lands first)**
- [x] Tool execution serialised behind one lock in `ToolExecutor`: unique call ids and
      one thread at a time on the run's DuckDB connection. `ResultStore` and
      `Trajectory` are lock-guarded; `seq` stays gap-free under threads.
- [x] `state.SharedBudget` and `state.RoleAllowance`: grants never exceed the pool,
      every charge counts against the run totals, `close()` returns unused capacity
      to the pool, `top_up()` hands it on. `role_wall_time` is a soft stop.
- [x] `state.LoopBudget` protocol; `Session.budget` accepts either budget.
- [x] `investigate.Brief` gains `hypothesis_id` (recorded on every event), `system`
      (per-role system prompt) and `tools` (allowlist, enforced by the loop).
- [x] `RunState`, `write_json` and `target` moved to `agent/run_state.py` so the
      multi graph can reuse them without importing the single graph.
- [x] Trajectory role vocabulary fixed: `agent | lead | tester | skeptic`, with
      `hypothesis_id` on tester events. This is the contract Phases 08 and 09 use.

*07b notes:* the roles live in `agent/roles.py` (terminal tools `dispatch`,
`record_verdict`, `record_challenge`, validated in code), the prompts in
`agent/prompts.py` (`PROMPT_VERSION` 07.1; per-role hashes in the multi manifest), and
the graph in `agent/multi_graph.py`. The disproval edge is implemented as the lead's
report step turning into one correction, so no second fan-out or second skeptic can
run. A tester's verdict counts only with a citation of a call it made itself
(found in the trajectory by role and hypothesis id). Testers and the skeptic get the
data tools and `read_result`; the ledger and `search_past_incidents` are the lead's.
If the lead runs out before dispatching, its non-rejected ledger hypotheses are briefed
by a fallback. `RuleClient` drives every role, so dry runs work in multi mode.

**Workstream B — Graph (`agent/multi_graph.py`)**
- [x] Graph: orient → lead → testers (fan-out, run in parallel) → merge → skeptic →
      report, with one edge from skeptic back to lead when the hypothesis is disproved.
- [x] Ledger writes from parallel testers merged through a reducer keyed by hypothesis
      id, so no verdict overwrites another.
- [x] One shared budget object: the total tool-call, model-turn, wall-time and cost
      limits are the same as the single agent's, split across roles by a configurable
      allocation. Default for 15 tool calls: lead 3, testers 3 each for up to three
      hypotheses, skeptic 2, and 1 held back for the lead's correction if the skeptic
      refutes. The skeptic's and correction's share is reserved before the fan-out.
- [x] Unused budget from a tester that finishes early returns to the shared pool.
- [x] Each tester may update only its own hypothesis. Verdicts are merged in
      hypothesis-id order, so the result does not depend on which tester finished first.
- [x] Partial fan-out is defined: a tester that times out, errors or runs out of budget
      leaves its hypothesis `undecided` with the reason recorded; ranking proceeds on
      the verdicts that exist, and an undecided hypothesis ranks below a supported one
      and above a rejected one.
- [x] Limits behave as in Phase 05: on exhaustion, a forced report from the ledger.
- [x] `agent.mode: single | multi` in the agent configuration; the mode is part of the
      run-cache key. *(07a: `AgentConfig.mode`, `skeptic_model_alias` and the
      `multi` allocation, in `to_dict` and so in the fingerprint.)*

**Workstream C — Run record and evaluation**
- [x] Every model and tool call in the trajectory is tagged with the role and, for
      testers, the hypothesis id.
- [x] The grounding check accepts evidence from any role's tool calls in the same run,
      and nothing from outside it.
- [x] `--mode` option on `run_eval.py`; `compare_models.py` gains a mode column so
      single and multi appear side by side.
- [x] Per-role breakdown in `failure_report.py`: tokens and tool calls by role, how
      often the skeptic overturned the lead, and whether it was right to.

*07c notes:* `--mode single|multi` sets `AgentConfig.mode` and sits beside `--agent`
(`single | single_call`), which still picks the agent versus the single-call baseline;
the mode enters the cache key through the agent configuration.
`compare_models.py --modes single multi` runs each model in both modes, adds a `mode`
column, writes results under `results/phase-07/`, and records a `selection` block per
model: the multi-minus-single service AC@1 gain with its paired 95% interval and the
mode Decision 4 selects. The agent adapter records usage by role and the skeptic's
outcome; `failure_report.py` adds `<role>_<metric>` columns and a skeptic summary
(challenged, disproved, and whether each disproval overturned a wrong answer, judged
against the tuning answer). Single-mode reports keep their Phase 06 columns.
DESIGN.md now states the bootstrap rule.

**Experiment (paid)**
- [x] Single versus multi on the 30 tuning cases: same model for every role, same
      candidate lists and time windows, same total limits. Report accuracy with
      intervals, the paired difference with its interval, and realised cost, tokens and
      wall time per case beside the equal caps.
- [x] Record the selection rule's outcome (Decision 4) in §5.

**Tests**
- [x] Unit tests: brief and verdict schemas, budget allocation and pool return, the
      ledger reducer under concurrent verdicts.
- [x] Graph tests with the scripted fake client: happy path with three testers; a tester
      that hits its limit; a tester whose tool errors; skeptic disproves and the run
      returns to the lead exactly once; shared budget exhausted mid-fan-out.
- [x] Test that the skeptic's input contains no message from the lead or the testers.
- [x] Test that total tool calls in multi mode never exceed the single-agent limit.
- [x] One `live` test: a single tuning case in multi mode on the cheapest model.
      *(First run failed, which led to Decision 8; passed after #32, $0.0074.)*
- [x] Keep `flowchart-multi-agent.svg` in sync with the graph as built.

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

#### Decision 6: Returned budget is handed out only after the fan-out

* **Decision:** A tester that finishes early returns its unused tool calls, turns and
  cost to the shared pool, but the pool is granted again only after every tester
  has finished: to the lead's report step. The skeptic keeps its fixed share.
* **Context/Constraint:** If a running tester could draw on what another returned,
  how much each hypothesis was tested would depend on which thread finished first,
  and two runs of the same case could differ for no reason in the data.
* **Trade-off:** A tester that needs one more call cannot borrow it from an idle
  sibling, so some capacity waits until the report.

#### Decision 8: The lead's hypotheses arrive with `dispatch`, at no tool-call cost

* **Decision:** A `dispatch` brief may add a hypothesis (component, statement,
  evidence) to the ledger, not only name one already there. Like `submit` in single
  mode (Phase 05 Decision 9), the terminal call does not count against the tool-call
  limit; its evidence passes the same citation check.
* **Context/Constraint:** In the first live run (gpt-4o-mini, one tuning case,
  $0.0008), the lead batched its three tool calls on data in one turn. Its
  `update_hypotheses` call was then refused at the limit, the ledger stayed empty, no
  tester ran, and the report was inconclusive. Recording a hypothesis is bookkeeping;
  with three calls the lead cannot afford to pay for it.
* **Trade-off:** The lead can record hypotheses without spending a call, which single
  mode cannot do mid-investigation. Totals still match single mode, whose `submit` is
  also free.
* **Rejected alternatives:** raising the lead to four calls, taken from the testers.
  It shrinks the testers' share and fails the same way for a model that batches four
  calls.

#### Decision 9: Validation failures get one correction, and briefs merge by component

* **Decision:** (a) A refused terminal call on a step's last turn gets one more
  terminal-only turn to correct it, in every role and in single mode. (b) Two
  dispatch briefs for one component are merged into one hypothesis and one tester
  instead of refusing the dispatch; the brief count is checked after merging.
  (c) `dependency_graph` gives each edge a quotable `caller -> callee` string, and
  the skeptic is told to copy quotes exactly.
* **Context/Constraint:** In the first comparison (§5) the lead's dispatch was
  refused for a repeated component in 8 of 30 cases, on its last turn, so no tester
  ran; the skeptic hit its limit in 15 of 20 cases after quoting graph edges in its
  own words; 21 `submit` calls were refused for fixable reasons.
* **Trade-off:** One extra model turn per refusal, inside the same turn and cost
  limits. Merged briefs give one tester two explanations to check. The change
  affects single mode as well, so single-mode results after this differ from §5's.

#### Decision 7: Tool calls are serialised; model calls run in parallel

* **Decision:** Testers run on real threads (LangGraph fan-out). Data-tool calls go
  through one lock in the shared executor; model calls overlap.
* **Context/Constraint:** The run has one read-only DuckDB connection and one
  call-id sequence that every citation refers to. Tool calls take milliseconds;
  model calls take seconds, so the wall-time gain is kept.
* **Trade-off:** Testers wait on each other's queries. Per-thread DuckDB cursors
  would remove that wait at the cost of a second id scheme; not worth it at these
  sizes.

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

**Equal-budget comparison, tuning set (30 cases, 10 service–fault pairs).** Run from
`main` at `20bac5b` with
`compare_models.py --models gpt-small --modes single multi --cases tuning --workers 4`;
gpt-4o-mini (served `gpt-4o-mini-2024-07-18`) for every role; same candidate lists,
time windows and per-case limits (15 tool calls, 30 model turns, $0.05). Result files:
`results/phase-07/real/239317c23c9e4c79a68407ad008d5724/`. Intervals: 95%, bootstrap
resampled by service–fault pair, seed 6, 10,000 draws.

| Arm | service AC@1 | service AC@3 | cost/case | tool calls/case | wall time/case |
|---|---|---|---|---|---|
| guarded BARO (reference, free) | 0.933 [0.833, 1.000] | 1.000 | $0 | 0 | — |
| single | 0.800 [0.600, 0.967] | 0.900 [0.767, 1.000] | $0.0075 | 11.5 | 64 s |
| multi | 0.433 [0.200, 0.667] | 0.567 [0.300, 0.800] | $0.0053 | 10.7 | 75 s |

Indicator AC@k was 0.000 for both agent arms.

**Decision 4 outcome: single mode is selected for the final run.** The paired gain of
multi over single in service AC@1 is −0.367 [−0.633, −0.133]: the whole interval is
below zero, so multi is not just "not shown better" but worse on tuning at this budget
with this model.

**Where multi lost (failure report on the multi run):** 13 correct top-1, 9 with no
ranking, 8 wrong after inspecting the root. 13 of 30 runs ended forced (10 at the tool
limit, 1 at the turn limit); 11 reports were inconclusive. The skeptic ran on only 5
cases (the other runs had no hypothesis with data support to challenge) and disproved
2, both of which were the true root cause, so both overturns were wrong. By role:
testers used 183 tool calls and 214 turns, the lead 99 calls, the skeptic 20. With 3
calls each, testers mostly returned `undecided`, as in the live run.

**Limitations, stated as Decision 3 requires:** one cheap model, one run per arm (run
noise not yet measured), and small per-role allowances. The result says the
architecture does not pay off at equal budget with gpt-4o-mini; it does not test a
larger budget or a stronger model. The run cost about $0.38 of the $1.50 allocation;
the spend guard's total for the project is $0.40.

## 6. AI-use notes (feeds `REPORT.md`)
- 07a: Claude Code mapped the thread-safety hazards in the shared run (registry call
  ids, `call_log[-1]`, budget counters, trajectory `seq`, store iteration), proposed
  the three-PR split, and wrote the locks, the shared budget, the config fields and
  their tests. The owner chose real threads over sequential testers and the
  bootstrap selection rule over DESIGN.md's simpler one.
- 07b: Claude Code wrote the role prompts, `roles.py`, `multi_graph.py`, the
  role-aware `RuleClient`, the routed scripted client for threaded tests and the tests
  (including one that proves the testers overlap, using a barrier). It found that
  parallel testers cannot cite fixed call ids in a script, so test turns can be
  functions of the request. No prompt was tuned on the tuning set; the role prompts
  are first drafts, to be judged by the Phase 07 comparison.
- 07f: Claude Code broke the comparison's failures down from the trajectories
  (refused dispatches, paraphrased graph quotes, refused submits) and proposed
  Decision 9; the owner chose fixes 1 and 2. `PROMPT_VERSION` 07.3.
- 07d: the first live multi run failed (see Decision 8); Claude Code diagnosed it
  from the trajectory, proposed letting `dispatch` add hypotheses, and the owner chose
  that over a larger lead allowance. `PROMPT_VERSION` 07.2.
- 07c: Claude Code wrote `--mode`, the mode arms and the selection rule in the
  comparison, the per-role usage in the adapter and the failure report columns, and
  their tests, including the rule at a clear gain, no gain, a mixed result whose
  interval spans zero, and too few groups for an interval.
