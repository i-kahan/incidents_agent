# Phase 17: Collective Signature Memory

**Status:** Not started. Depends on Phases 09 and 13.

## 1. Objective
Let users opt in to share anonymised failure signatures, so that the tool can say "this
pattern was confirmed as a connection-pool exhaustion by most of the people who saw
it". Matching happens on the user's machine against a downloaded index; no query about
the user's files leaves it.

## 2. Next Steps (Checklist)

**Workstream A: Signatures**
- [ ] A signature built from a confirmed Phase 09 record: masked message patterns,
      fault type, the shape of the metric changes (which kind of column moved, which
      way, in what order), and component roles. No values, hostnames, ids or free text.
- [ ] The user sees the exact signature before it is shared, and sharing is per record.

**Workstream B: The index**
- [ ] A hosted index that accepts signatures and publishes a downloadable snapshot,
      with per-signature counts of confirmed and rejected diagnoses.
- [ ] The tool downloads the snapshot and matches locally; no lookup is sent.
- [ ] `search_past_incidents` returns matches from the shared index as a separate list,
      with their counts.

**Workstream C: Trust**
- [ ] Only signatures from runs whose report passed the citation checks and whose
      outcome the user confirmed can be shared.
- [ ] Limits per contributor, signed contributions, and counts shown instead of
      claims, so one contributor cannot dominate a pattern.
- [ ] The Phase 09 rule holds: a shared match is a lead, never an evidence item.

**Tests**
- [ ] A signature contains no value from a canary fixture.
- [ ] Local matching works offline against a snapshot.
- [ ] A flood of signatures from one contributor does not change the ranking beyond the
      limit.
- [ ] Update `flowchart-system-overview.svg` for the shared index.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Download the index, match locally

* **Decision:** Users download the whole index and match on their machine.
* **Context/Constraint:** A lookup service would learn what each user is debugging.
* **Trade-off:** The index must stay small enough to download, which limits signature
  detail.

## 4. Constraints

* **Opt in only.** *Source: owner.* **Implication:** nothing is shared by default, and
  sealed mode (Phase 13) never shares.
* **⚠️ OPEN:** who hosts the index, and the licence for contributed signatures.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
