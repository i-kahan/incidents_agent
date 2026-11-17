# Phase 16: Fault Gym and Pre-Mortem

**Status:** Not started. Depends on Phases 11 and 12; reuses the Phase 06 scorer.

## 1. Objective
Let a user find out how well the tool works **on their own data** before relying on it.
The fault gym takes the user's healthy files, makes copies with known faults injected,
runs the investigation on each, and reports how many it found. The pre-mortem uses the
same machinery on healthy files to report which kinds of failure these files could not
show at all, and what to collect so they could.

## 2. Next Steps (Checklist)

**Workstream A: Fault injection**
- [ ] A catalogue of injectors, each working on generic tables, logs and documents:
      latency or value step on a numeric column; error-rate burst in logs; a source
      going silent; a gradual resource ramp; a changed config value; a restart loop;
      a fault in one component that appears in a caller after a delay.
- [ ] Each injection picks a component, a time and a size from a seed, writes a
      modified copy, and records the answer in a separate answers file the agent
      cannot read (same rule as the benchmark).
- [ ] Injectors only use components and columns found in the user's data.

**Workstream B: The gym**
- [ ] `why gym <folder>`: runs N injected copies (default 20) through triage, free.
- [ ] `--investigate` runs the agent on them too, with the cost estimate printed first
      and the user's spend limit in force.
- [ ] Report: found N of M, by fault type and by size, with the missed cases listed and
      replayable.
- [ ] Scoring reuses the Phase 06 scorer and statistics.

**Workstream C: Pre-mortem**
- [ ] `why premortem <folder>`: for each fault type, whether triage detects it at
      realistic sizes in this data (from a triage-only gym run).
- [ ] Static checks that need no injection: sources with no usable time column;
      sampling too coarse for short faults; logs with no level; no relationships for
      `dependency_graph`; components named in logs that have no metrics.
- [ ] Output: "if X broke, these files could not show it", each with what to collect
      to fix that.

**Tests**
- [ ] Each injector on fixtures: the modified copy differs only where intended, and
      the recorded answer matches.
- [ ] Seeds make a gym run reproducible.
- [ ] The agent's input path never contains the answers file.
- [ ] Pre-mortem on a fixture with no metrics reports the resource faults as invisible.
- [ ] Add a diagram if the gym gets its own flow (`flowchart-fault-gym.svg`).

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Faults are injected into copies of the user's own data

* **Decision:** The gym modifies the user's healthy files rather than shipping a fixed
  synthetic system.
* **Context/Constraint:** The benchmark measures one application. Users want to know
  how the tool does on theirs.
* **Trade-off:** Injected faults are cleaner than real ones. The gym measures how
  sensitive the tool is on this data's shape, not its accuracy on real incidents, and
  its report says so.

#### Decision 2: Free by default

* **Decision:** Gym and pre-mortem run triage only unless the user asks for the agent.
* **Context/Constraint:** Twenty agent runs cost real money on the user's key.
* **Trade-off:** The default result measures triage, not the agent; the report labels
  which was measured.

## 4. Constraints

* **Users bring their own model keys.** *Source: owner.* **Implication:** paid gym runs
  show the estimate first and respect the spend guard.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
