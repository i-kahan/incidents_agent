# Phase 11: Triage Without a Model, and the `why` Command

**Status:** Not started. An additional feature; starts after the core pipeline's
end-to-end milestone (see the [index](README.md)).

## 1. Objective
Answer "what stands out in these files?" in seconds, with no model and no key, and make
that answer the first thing every user sees. The same triage is offered to the agent as
a tool, and, once measured, feeds its briefing. A small `why` command makes the whole
path usable from a terminal. Ends with triage running before every investigation in the
CLI and the app, and `why <folder>` working with no configuration at all.

## 2. Next Steps (Checklist)

**Workstream A: Triage (`triage/`)**
- [ ] A deterministic triage pass over the ingested database, built from the Phase 03
      tools rather than new statistics:
      - first error and warning lines per source, with their times;
      - log message patterns that are new, or whose rate changed most, after the
        incident time (or the `find_onset` estimate when no time is given);
      - the numeric columns that shift most across that time (`compare_windows`);
      - gaps: sources that go silent, or start, near the incident time;
      - one merged timeline of the first notable events across all sources;
      - when `dependency_graph` finds relationships, the components that changed
        earliest in the graph.
- [ ] Every triage item carries a locator (table and row, or file and line), so the
      agent and the report can cite it like any other tool result.
- [ ] Result follows the Phase 03 result contract: capped, truncation notice, errors
      returned as data.
- [ ] Rendered as Markdown by a plain function, with unit tests.

**Workstream B: Where triage runs**
- [ ] The CLI and the upload page run triage before any model call and show it first,
      including when no model is configured.
- [ ] A `triage` tool in the registry, so the agent can ask for it.
- [ ] `agent.triage_in_briefing` flag: puts the triage summary into the orient
      briefing. Off by default until measured (below). Part of the run-cache key.
- [ ] Triage scored alone on the tuning cases as a free row in the baseline ladder:
      is the answer the top-ranked shifted component? Held-out cases only through the
      Phase 06 gate.

**Workstream C: The `why` command (optional entry point)**
- [ ] A console entry point `why` in `pyproject.toml`, over the same run function as
      the CLI and the app.
- [ ] Accepts a folder, one or more files, or standard input
      (`kubectl logs pod | why`).
- [ ] Default is triage only, free and offline. `--investigate` runs the agent with
      the user's configured model, after printing the cost estimate and limit.
- [ ] Options: `--at <time>` for the incident time, `--reference <folder>` once Phase 12
      exists, `--out <dir>` for the report and trajectory.
- [ ] Output readable in an 80-column terminal; `--format markdown|json`.

**Experiment (paid, ranked with the other paid uses)**
- [ ] Agent with and without `triage_in_briefing` on the tuning cases and the field
      set, with the Phase 06 paired statistics. The flag's default follows a rule
      fixed before the run: on only if top-1 does not drop and cost per case falls.

**Tests**
- [ ] Triage on the synthetic fixture folder: each section finds the planted signal,
      and every locator resolves to the row or line it names.
- [ ] Triage on a healthy fixture returns no error-level findings.
- [ ] `why` end to end on a fixture folder and on standard input, with no keys set.
- [ ] `--investigate` with the fake client produces the same report as the CLI.
- [ ] Update `flowchart-system-overview.svg` and `flowchart-agent-graph.svg` for the
      triage step.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Triage is deterministic and reuses the existing tools

* **Decision:** Triage is a fixed sequence of Phase 03 tool calls and summaries. No
  model is involved.
* **Context/Constraint:** Users bring their own keys, and many will try the tool before
  configuring one. The statistical tools are already strong on the benchmark.
* **Trade-off:** Triage cannot separate cause from symptom; it says what changed, not
  why. The report labels it that way.

#### Decision 2: Triage reaches the agent as a tool first, and the briefing only after measurement

* **Decision:** The `triage` tool is available from the start; putting triage in the
  briefing waits for the experiment above.
* **Context/Constraint:** Changing what the agent sees at the start changes every
  benchmark number. It has to be measured like any other agent change.
* **Trade-off:** Until measured, the agent may spend a tool call fetching what the user
  already saw.

#### Decision 3: `why` is a thin entry point

* **Decision:** `why` calls the same run function as the CLI, the app and the eval
  runner; it adds argument parsing and terminal rendering only.
* **Context/Constraint:** The benchmark numbers are evidence about the product only if
  every entry point runs the same code (Phase 08, Decision 2).
* **Trade-off:** A shell hook that explains the last failed command is left out; it
  needs per-shell work and is listed in
  [`future-improvements.md`](../future-improvements.md).

## 4. Constraints

* **Users bring their own model keys.** *Source: owner.* **Implication:** everything in
  this phase works without a key; the model is an upgrade the user turns on.
* **Incident time is optional.** *Source: the nature of the input.* **Implication:**
  triage uses the `find_onset` estimate and says so.
* **New features come after the end-to-end path.** *Source: owner.* **Implication:**
  this phase starts after the end-to-end milestone. It is the first of the new phases
  because Workstreams A and C change nothing the agent sees.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
