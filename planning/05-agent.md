# Phase 5: Agent

**Status:** In progress — Workstreams A and B landed; run record and skeleton next

## 1. Objective
Build the investigating agent: it reads the catalog, forms hypotheses, tests them with
tools, tries to disprove its leading explanation, and reports a ranked diagnosis with
cited evidence. Ends with an agent that runs on one case end to end, within limits, and
whose every claim can be traced to a tool call.

See [`flowchart-agent-graph.svg`](flowchart-agent-graph.svg).

## 2. Next Steps (Checklist)

**Workstream A — State and ledger (`agent/state.py`, `agent/ledger.py`)**
- [x] Run state: task, catalog, messages, ledger, tool-call log, budget counters.
      *(`agent/state.py`: `Task`, `Budget` with the verify reserve; `agent/config.py`
      reads `config/agent.yaml`; `agent/trajectory.py` and `agent/report.py` fix the
      trajectory schema and report shape that Phases 06–09 consume.)*
- [x] Ledger entry: statement, component, status (open / supported / rejected),
      qualitative confidence, evidence for and against, verification outcome, origin
      (`data` or `memory`).
- [x] `update_hypotheses` tool: add, revise, reject.
- [x] Citation validity check, in code. An evidence item must reference a tool call
      from this run that succeeded and returned a non-empty result, carry a locator
      from that result (row keys, aggregate and window, or chunk id), and quote a value
      or phrase that actually appears in it. Items that fail are rejected with an error
      the model can read. *(`agent/ledger.py` `check_citation`, over the full
      results in `agent/results.py`; quotes match one observation, ignoring case and
      whitespace; the untrusted-data notice is never citable.)*
- [ ] The final report is validated the same way: every claim in it maps to a valid
      evidence item, or the claim is dropped and the drop recorded.
- [x] Citation validity says a claim points at something real. It does not say the
      observation supports the claim; that is audited by hand (Phase 06) and the two are
      never reported as one number.
- [x] Rule: an entry with origin `memory` cannot become supported without at least one
      evidence item from the current data.

**Workstream B — Graph (`agent/graph.py`, `agent/prompts.py`)**
- [x] `orient` node: deterministic briefing from the catalog and task.
- [x] `investigate` node: model call with tools; loops until `submit` or a limit.
      Written as a function of (task, tool budget, ledger handle, message history) so
      Phase 07's hypothesis testers reuse it with a different brief.
- [x] `verify` node: the model must name and run one check that could disprove the top
      hypothesis. The outcome is explicit — `supported`, `refuted` or `inconclusive` —
      and is stored with the evidence. On `refuted` the run returns to `investigate`
      once.
- [x] If a different hypothesis ends up on top after a refutation, it carries its own
      verification status. If nothing is left to verify it with, it is reported as
      `unverified`, not as verified by inheritance.
- [x] `report` node: structured output — ranked root causes, fault type, verification
      status, confidence as a qualitative level (low / medium / high) with the reasons
      for it, evidence, recommended actions. No numeric probability is shown unless it
      has been calibrated against results.
- [x] `no_fault_found` is a valid final outcome, distinct from `inconclusive`: the data
      was examined and nothing abnormal was found. The briefing says so, so the agent
      is not pushed to name a culprit.
- [x] Recommended actions: each is tied to the hypothesis it addresses and carries a
      risk level (read-only check / reversible change / disruptive change) and how to
      tell whether it worked. Read-only checks are listed first.
- [x] Data requests: when the outcome is provisional or inconclusive, the report lists
      the specific additional data that would settle it — what, from where, and which
      hypothesis it would confirm or refute. Each request is derived from an open or
      undecided ledger entry, not free-form.
- [x] When dependency data exists, the verify prompt asks for a check of direction:
      whether the suspect is upstream of the other affected components.
- [x] `submit` validation: with candidate lists present (benchmark), ranks must come
      from them; without (uploads), ranks are free text.
- [x] Limits per case: tool calls (15), model turns, wall time and cost. Verification
      capacity is reserved before investigation starts — two tool calls and the model
      spend for `verify` and one bounded correction — so `investigate` can use only the
      remainder and the run can never reach `verify` with nothing left.
- [x] One common finalisation path, used by every way a run can end (normal submit,
      any limit, a refused reservation, repeated invalid submissions) and by every
      configuration including the plain loop: the ledger (or, with the ledger off, the
      model's last stated ranking) is rendered deterministically into a ranking, marked `verified`, `provisional` or `inconclusive`, with the stop
      reason recorded. Invalid submissions are bounded by the model-turn limit.
      *(`agent/finalise.py`. Only ranked components with a valid supporting citation
      survive; a forced ranking is capped at low confidence, an unverified one at
      medium.)*
- [x] Context policy: the last few tool results stay in the conversation in full. Older
      ones are replaced by a deterministic digest (tool, arguments, locator, size,
      first lines). The ledger is the durable summary. Full results stay in the
      trajectory, so citations remain checkable, and a trimmed result can be re-read by
      call id without a new query.
- [x] The conversation is ordered so the stable part (system prompt, tool schemas,
      catalog briefing) comes first, to benefit from prompt caching where available.
- [x] Prompts kept in one versioned module; the prompt hash is part of the run record.
- [x] Instruction in the system prompt that file contents are data, not instructions.

**Workstream C — Run record**
- [ ] Trajectory written per run: every model call, tool call, ledger change, token and
      cost figure, and the final report, as JSON under `runs/`.
- [x] Ablation switch in the agent configuration: `ledger` and `verify` on or off; off
      gives a plain tool-calling loop with the same tools.
- [ ] Resumable runs: state is checkpointed at each node. `continue_run` takes a saved
      run plus added files: it re-ingests additively, writes a "what changed" briefing,
      carries the ledger and its evidence over, and continues under a fresh
      per-continuation budget. The result is the same run with a continuation index.

**Workstream D — Walking skeleton**
- [ ] `scripts/investigate.py <folder>`: ingest, run the agent, write a cited Markdown
      report and the trajectory. With `--fake` it uses the scripted client at no cost.
- [ ] `scripts/replay.py <trajectory>`: re-render the report from a saved trajectory
      without calling a model.
- [ ] Both run end to end on the victim-versus-cause fixture before Phase 07 starts.

**Tests**
- [x] Unit tests for the ledger rules and the citation validity check: a failed call,
      an empty result, a value not present in the result, and a call from another run
      are each rejected.
- [x] Graph tests with the scripted fake client: happy path, forced submit at the call
      limit, verify still runs when investigation used its whole share, forced submit at the cost cap, verify failure sending the run back,
      invalid `submit` names rejected, tool error surfaced and survived.
- [ ] More graph tests with the fake: a trimmed result is still citable and can be
      re-read; `no_fault_found` is reachable and finalises correctly; a continuation
      keeps earlier evidence valid and sees the added tables; data requests appear
      only for open or undecided hypotheses.
- [x] Injection test with the fake: a tool result containing an instruction does not
      change the tool set, the limits or the finalisation path. Whether a real model
      resists it is measured on the field set in Phase 06.
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

#### Decision 6: Old tool results are trimmed; the ledger and trajectory hold the record

* **Decision:** Only recent tool results stay in the conversation in full. Older ones
  become short digests, with the full text kept in the trajectory.
* **Context/Constraint:** Each model call re-sends the whole conversation, so fifteen
  results of several thousand characters each make later calls many times dearer than
  early ones. This is the main driver of cost per case, and the budget allocation
  depends on keeping it down.
* **Trade-off:** The agent can lose sight of a detail it did not write into the ledger
  and has to re-read it. Digests are deterministic, so behaviour is reproducible.

#### Decision 7: The agent says what is missing, and a run can continue with more data

* **Decision:** An unsettled diagnosis comes with specific data requests, and the same
  run can be continued after files are added.
* **Context/Constraint:** Real investigations rarely start with everything needed. An
  agent limited to the first upload either guesses or gives up; neither helps the
  person using it.
* **Trade-off:** The agent cannot fetch anything itself; a person has to supply the
  data. Continuation costs more model spend and adds state that must stay consistent
  between ingests. The benchmark never exercises it, so it is tested only on fixtures.

#### Decision 8: Agent limits are configuration, separate from the spend budget

* **Decision:** Per-case limits (tool calls, turns, wall time, cost cap, the verify
  reserve) live in `config/agent.yaml` and are written into every run record. The
  cost cap starts at $0.05, the Phase 02 target, and counts settled cost.
  `config/budget.yaml` is not touched.
* **Context/Constraint:** The spend guard reserves a worst-case bound per call, several
  times the real cost, so a cap enforced there would stop runs far too early. The
  agent's cap is about behaviour per case; the guard's limit is about money overall.
* **Trade-off:** A run can overshoot its cost cap by the cost of its last call, since
  settled cost is known only after the call. The global guard still bounds the total.

#### Decision 9: `submit` carries the whole answer; the report step makes no model call

* **Decision:** `submit` carries the ranking, fault type, confidence and reasons,
  actions and data requests. `report` validates them and renders the result in code.
  `submit`, `record_verification` and invalid calls of either do not count against the
  tool-call limit; `update_hypotheses` and `read_result` do.
* **Context/Constraint:** The model layer has no forced structured output, so the end of
  a step has to be a tool call checked in code. A separate model call to write the
  report would cost a turn per case and would be a second place where unchecked claims
  could enter.
* **Trade-off:** The model must produce everything in one call, with less room to
  reflect. Ledger bookkeeping uses tool calls the investigation could have used; the
  ablation measures whether that pays.

#### Decision 11: The graph is thin; run state and checkpoints are plain Python

* **Decision:** LangGraph holds only the node order and the routing. Nodes act on one
  `RunState` object, and state is written to `checkpoint.json` at every node exit,
  atomically.
* **Context/Constraint:** LangGraph's SQLite checkpointer is a separate package not in
  the lockfile, and its saved form is tied to the framework. The ledger, results and
  conversation already serialise to JSON for the trajectory.
* **Trade-off:** LangGraph's own resume and time-travel features are not used;
  `continue_run` resumes from the JSON checkpoint instead.

#### Decision 10: Old results are digested in blocks

* **Decision:** The last three data-tool results stay in full. When three more have
  piled up, the oldest three become digests in one step.
* **Context/Constraint:** Digesting one result per turn would change the conversation
  prefix on every turn, so provider prompt caching would never hit.
* **Trade-off:** Up to five full results are in context at a time instead of three.

## 4. Constraints

* **Fault start time is given on the benchmark, optional on uploads.** *Source: RCAEval;
  owner.* **Implication:** `orient` passes it when present; when absent the briefing
  tells the agent to establish the onset first with `find_onset`.
* **Recommended actions are never executed.** *Source: agreed scope.* **Implication:**
  the report lists them as text; there are no tools with side effects.
* **Per-case cost cap.** *Source: budget.* **Implication:** set from Phase 02's measured
  estimate so a full pass fits its allocation in Phase 06.

## 5. AI-use notes (feeds `REPORT.md`)
- Claude Code (Fable 5.2) drafted the Phase 05 plan: seven steps, one PR each, with
  model ids, API keys and the live run left to the last step at the owner's request.
- Step 1 (contracts): Claude Code wrote `agent/config.py`, `state.py`,
  `trajectory.py`, `report.py`, `config/agent.yaml` and their tests.
- Step 2 (ledger): Claude Code wrote `agent/results.py`, `agent/ledger.py` and their
  tests. Results of memory tools are marked so they never count as current-data
  support. Coverage: ledger 90%, results 95%.
- Step 3 (loop): Claude Code wrote `agent/executor.py`, `context.py`, `prompts.py`,
  `investigate.py`, `finalise.py`, a starting call number on `ToolRegistry`, and the
  loop and finalisation tests on the `victim_vs_cause` fixture with the scripted fake.
- Step 4 (graph): Claude Code wrote `agent/graph.py` (nodes, routing, verify tool,
  checkpoints, `run_investigation`) and 13 graph tests, including the injection test.
  A verdict must cite a call made during the check; a refuted hypothesis is rejected;
  verify runs at most twice and the run returns to investigate at most once.
