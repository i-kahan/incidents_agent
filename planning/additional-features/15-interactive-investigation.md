# Phase 15: Interactive Investigation

**Status:** Not started. Depends on Phase 08 (core) and Phase 11.

## 1. Objective
Let the user see how an investigation unfolded over time and steer it. The incident
scrubber shows the data and the agent's reasoning on one timeline. The editable ledger
lets the user reject, pin or add hypotheses and continue the run. Both are added to the
Phase 08 upload page.

## 2. Next Steps (Checklist)

**Workstream A: Incident scrubber**
- [ ] One timeline for a run, with the cited metrics, the log pattern counts and the
      agent's ledger drawn together.
- [ ] Moving the cursor shows the data at that time, and the hypotheses as they stood
      at that step of the run.
- [ ] Built on the Phase 08 past-runs viewer; no model call.

**Workstream B: Editable ledger**
- [ ] On a finished or paused run, the user can reject a hypothesis ("it is not DNS"),
      pin one to be tested first, or add one, then continue the run through the
      Phase 05 continuation path.
- [ ] User edits are ledger entries with a new origin tag, `user`.
- [ ] A hypothesis the user added or pinned still needs evidence from the current data
      before it can be marked supported, as with origin `memory`.
- [ ] A user rejection is recorded with its reason and shown in the report.
- [ ] The same edits are available from the CLI (`--reject`, `--pin`, `--add` on
      continue).

**Tests**
- [ ] The scrubber's state at a given step matches the replayed ledger.
- [ ] A continued run after a user rejection does not propose the rejected hypothesis
      again without new evidence.
- [ ] A pinned hypothesis without evidence stays unsupported.
- [ ] Update `flowchart-agent-graph.svg` for the `user` origin.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: User edits go through the ledger, not the prompt

* **Decision:** A user's rejection, pin or addition is a ledger entry with origin
  `user`, subject to the same checks as any other entry.
* **Context/Constraint:** The ledger's citation checks are what make a report
  trustworthy; a user's opinion must not bypass them.
* **Trade-off:** A user who knows the answer still has to wait for the agent to find
  evidence for it.

## 4. Constraints

* **No agent logic in the app.** *Source: Phase 08, Decision 2.* **Implication:** the
  `user` origin and its rules live in the agent package; the page only sends edits.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
