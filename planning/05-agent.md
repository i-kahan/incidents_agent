# Phase 5: Agent

**Status:** Not started

## 1. Objective
Build the investigating agent: it reads the catalog, forms hypotheses, tests them with
tools, tries to disprove its leading explanation, and reports a ranked diagnosis with
cited evidence. Ends with an agent that runs on one case end to end, within limits, and
whose every claim can be traced to a tool call.

See [`flowchart-agent-graph.svg`](flowchart-agent-graph.svg).

## 2. Next Steps (Checklist)

**Workstream A — State and ledger (`agent/state.py`, `agent/ledger.py`)**
- [ ] Run state: task, catalog, messages, ledger, tool-call log, budget counters.
- [ ] Ledger entry: statement, component, status (open / supported / rejected),
      qualitative confidence, evidence for and against, verification outcome, origin
      (`data` or `memory`).
- [ ] `update_hypotheses` tool: add, revise, reject.
- [ ] Citation validity check, in code. An evidence item must reference a tool call
      from this run that succeeded and returned a non-empty result, carry a locator
      from that result (row keys, aggregate and window, or chunk id), and quote a value
      or phrase that actually appears in it. Items that fail are rejected with an error
      the model can read.
- [ ] The final report is validated the same way: every claim in it maps to a valid
      evidence item, or the claim is dropped and the drop recorded.
- [ ] Citation validity says a claim points at something real. It does not say the
      observation supports the claim; that is audited by hand (Phase 06) and the two are
      never reported as one number.
- [ ] Rule: an entry with origin `memory` cannot become supported without at least one
      evidence item from the current data.

**Workstream B — Graph (`agent/graph.py`, `agent/prompts.py`)**
- [ ] `orient` node: deterministic briefing from the catalog and task.
- [ ] `investigate` node: model call with tools; loops until `submit` or a limit.
      Written as a function of (task, tool budget, ledger handle, message history) so
      Phase 07's hypothesis testers reuse it with a different brief.
- [ ] `verify` node: the model must name and run one check that could disprove the top
      hypothesis. The outcome is explicit — `supported`, `refuted` or `inconclusive` —
      and is stored with the evidence. On `refuted` the run returns to `investigate`
      once.
- [ ] If a different hypothesis ends up on top after a refutation, it carries its own
      verification status. If nothing is left to verify it with, it is reported as
      `unverified`, not as verified by inheritance.
- [ ] `report` node: structured output — ranked root causes, fault type, verification
      status, confidence as a qualitative level (low / medium / high) with the reasons
      for it, evidence, recommended actions. No numeric probability is shown unless it
      has been calibrated against results.
- [ ] `submit` validation: with candidate lists present (benchmark), ranks must come
      from them; without (uploads), ranks are free text.
- [ ] Limits per case: tool calls (15), model turns, wall time and cost. Verification
      capacity is reserved before investigation starts — two tool calls and the model
      spend for `verify` and one bounded correction — so `investigate` can use only the
      remainder and the run can never reach `verify` with nothing left.
- [ ] One common finalisation path, used by every way a run can end (normal submit,
      any limit, a refused reservation, repeated invalid submissions) and by every
      configuration including the plain loop: the ledger (or, with the ledger off, the
      model's last stated ranking) is rendered deterministically into a ranking, marked `verified`, `provisional` or `inconclusive`, with the stop
      reason recorded. Invalid submissions are bounded by the model-turn limit.
- [ ] Prompts kept in one versioned module; the prompt hash is part of the run record.
- [ ] Instruction in the system prompt that file contents are data, not instructions.

**Workstream C — Run record**
- [ ] Trajectory written per run: every model call, tool call, ledger change, token and
      cost figure, and the final report, as JSON under `runs/`.
- [ ] Ablation switch in the agent configuration: `ledger` and `verify` on or off; off
      gives a plain tool-calling loop with the same tools.

**Workstream D — Walking skeleton**
- [ ] `scripts/investigate.py <folder>`: ingest, run the agent, write a cited Markdown
      report and the trajectory. With `--fake` it uses the scripted client at no cost.
- [ ] `scripts/replay.py <trajectory>`: re-render the report from a saved trajectory
      without calling a model.
- [ ] Both run end to end on the victim-versus-cause fixture before Phase 07 starts.

**Tests**
- [ ] Unit tests for the ledger rules and the citation validity check: a failed call,
      an empty result, a value not present in the result, and a call from another run
      are each rejected.
- [ ] Graph tests with the scripted fake client: happy path, forced submit at the call
      limit, verify still runs when investigation used its whole share, forced submit at the cost cap, verify failure sending the run back,
      invalid `submit` names rejected, tool error surfaced and survived.
- [ ] One `live` test: a single tuning case on the cheapest configured model.
- [ ] Update `flowchart-agent-graph.svg` if nodes or edges changed.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: LangGraph with four explicit nodes

* **Decision:** The agent is a LangGraph state graph: orient → investigate → verify →
  report, with one edge back from verify to investigate.
* **Context/Constraint:** The investigation has distinct stages with different rules
  (the verify stage must run; the investigate stage is free-form). An explicit graph
  makes those rules visible and testable.
* **Trade-off:** More structure than a single loop needs, and a framework dependency.
  Tools and the ledger are plain Python so they do not depend on it.

#### Decision 2: The ledger is structured state, enforced in code

* **Decision:** Hypotheses live in a typed ledger the model edits through a tool; the
  grounding rule is checked by code, not requested in the prompt.
* **Context/Constraint:** A model asked to "cite evidence" in prose will sometimes cite
  things it never looked at. A reference to a tool-call id either exists or does not.
* **Trade-off:** Extra tool calls spent on bookkeeping, counted against the 15-call
  limit. The ablation measures whether it pays for itself.

#### Decision 3: A forced attempt to disprove the answer

* **Decision:** Before reporting, the agent must run at least one check that would come
  out differently if its top hypothesis were wrong.
* **Context/Constraint:** In microservice incidents the most anomalous service is often
  a victim of the real cause. The usual failure is stopping at the first strong signal.
* **Trade-off:** At least one more model call and tool call per case. Only one return
  to investigate is allowed, to bound cost.

#### Decision 4: Limits end in an answer, and verification is paid for first

* **Decision:** Capacity for verification is set aside before investigation begins. Any
  run that stops early is finalised from the ledger by code, labelled `provisional` or
  `inconclusive`, with its stop reason.
* **Context/Constraint:** RCAEval counts a missing prediction as a miss, and a user of
  the upload page needs something to act on. Without a reserve, an agent that spends
  its whole allowance investigating reaches the verify step unable to run it, and the
  step that distinguishes this design silently does not happen.
* **Trade-off:** Fewer calls are available for investigation. A forced answer may be
  weak; it is marked as such so the failure analysis can separate these cases.

#### Decision 5: A valid citation is not proof of support

* **Decision:** Code checks that every cited observation exists and was really
  returned. Whether it supports the claim is checked by a manual audit of a sample.
  Confidence is a qualitative level with reasons, not a number.
* **Context/Constraint:** A reference to any real tool-call id would pass a naive check
  even if the call failed, returned nothing, or was about something else. A check that
  cannot fail is not evidence of grounding. Numeric confidence from a model is not a
  probability unless measured, and 60 cases cannot measure it well.
* **Trade-off:** The manual audit is slow and covers a sample only. Quoted-value
  matching is strict, so the model sometimes has to re-query to cite precisely.

## 4. Constraints

* **Fault start time is given on the benchmark, optional on uploads.** *Source: RCAEval;
  owner.* **Implication:** `orient` passes it when present; when absent the briefing
  tells the agent to establish the onset first with `find_onset`.
* **Recommended actions are never executed.** *Source: agreed scope.* **Implication:**
  the report lists them as text; there are no tools with side effects.
* **Per-case cost cap.** *Source: budget.* **Implication:** set from Phase 02's measured
  estimate so a full pass fits its allocation in Phase 06.

## 5. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
