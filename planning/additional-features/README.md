# Additional Features

Features that widen the tool from operations incidents to anyone debugging a set of
files. They are separate from the core pipeline (Phases 01–10, planned in the
[hub](../00-high-level-architecture-planning.md)), and none of them is needed for it.

## When they start

Not before the core pipeline's **end-to-end milestone**: a folder of files goes in
through the CLI and the upload page in single mode and comes out as a cited report,
and the eval runner scores that same path on the tuning cases. That needs Phase 05,
the Phase 06 runner and Phase 08 single mode.

After the milestone:

- **Phase 11's triage report and `why`** may start at once, because they change nothing
  the agent sees.
- **Everything else starts after Phase 10**, so the final held-out run measures the
  core agent as planned.
- **A feature that changes what the agent sees** (triage in the briefing, sealed mode,
  the reference set) measures that change on the tuning cases and the field set before
  it becomes a default.

## Phases, in priority order

Ordered by how many people each helps, against its cost and risk.

| # | Phase | Depends on | What it adds | Status |
|---|---|---|---|---|
| 11 | [Triage without a model, and `why`](11-triage-and-why.md) | end-to-end milestone | A free, no-key report of what stands out; runs before the model and as a tool; optional `why` command | Not started |
| 12 | [Good versus bad comparison](12-good-vs-bad.md) | 11 | An optional known-good set to compare against, for when there is no incident time | Not started |
| 13 | [Sealed mode and where data lives](13-sealed-mode.md) | 11 | Sealed by default for downloaded copies; bring your own database; hosted example page | Not started |
| 14 | [Shareable outputs](14-shareable-outputs.md) | 11 (parts use 12, 13) | Evidence bundle, explanation notebook, coverage map, link to code; each optional | Not started |
| 15 | [Interactive investigation](15-interactive-investigation.md) | 08, 11 | Incident scrubber; editable ledger | Not started |
| 16 | [Fault gym and pre-mortem](16-fault-gym-and-pre-mortem.md) | 11, 12 | Measured accuracy on the user's own data; what the data cannot show | Not started |
| 17 | [Collective signature memory](17-collective-signature-memory.md) | 09, 13 | Opt-in shared failure signatures, matched locally | Not started |

## Constraints (owner)

- Users bring their own model keys; triage, gym and pre-mortem are useful with none.
- Downloaded copies are sealed by default; the hosted example page stores uploads in its
  database.
- The reference set, notebook, coverage map, link to code and `why` are optional.
- Shared signatures are opt in.

## Open items

- **Meaning of sealed mode** (13): whether it limits what the model sees, or only where
  data is stored. The plan assumes both.
- **Hosting** (13, 17): where the hosted example page and the shared signature index
  run, retention, and the licence for shared signatures.

## Conventions

The same as the core phases: one file per phase with the standard sections, one PR per
phase on a `feature/phase-NN-<name>` branch, tests with the code, and the SVGs in
[`planning/`](..) updated when a component is added. `REPORT.md` covers the core
pipeline; results from these phases go in their own phase files.
