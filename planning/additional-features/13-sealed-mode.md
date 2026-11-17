# Phase 13: Sealed Mode and Where Data Lives

**Status:** Not started. Depends on Phase 11.

## 1. Objective
Make it safe to point the tool at sensitive files. A copy of the tool that a user
downloads or clones runs **sealed by default**: files and results stay on the user's
machine, and the model receives only what sealed mode allows. A user can point the
agent at their own SQL database instead of dropping files. The hosted example page is
the one place where uploads are stored, in its own database, with stated retention.

## 2. Next Steps (Checklist)

**Workstream A: Sealed mode**
- [ ] `privacy.mode: sealed | open` in configuration. Sealed is the default for a local
      install; the mode is recorded in every run and is part of the run-cache key.
- [ ] Nothing is written outside the configured data folder, and nothing is sent
      anywhere except the model provider the user configured.
- [ ] Each tool returns its sealed result, as in the table below.
- [ ] Masking is deterministic (the same value always becomes the same token), so the
      agent can still follow one id across tables, and the report shows the real
      values locally by reversing the tokens on the user's machine.
- [ ] Citation checks run on the masked result the model saw.
- [ ] The report says which mode the run used.

What the model may see in sealed mode:

| Tool | Sealed result |
|---|---|
| `list_tables`, `describe_table` | Schema, counts, statistics; sample rows masked |
| `run_sql` | Aggregates in full; row results with text values masked, numbers and times kept |
| `search_text` | Message patterns with variable parts masked, with counts |
| `compare_windows`, `time_series`, `find_onset`, `dependency_graph`, `triage` | Unchanged (already aggregates); component names kept |
| `search_documents`, `read_document` | Chunks with secrets and personal data masked |

**Workstream B: Bring your own database (optional)**
- [ ] Point the agent at a read-only SQL database (Postgres, MySQL or SQLite) instead of
      files. The catalog is built from it, and the same tools run against it.
- [ ] Connected through DuckDB's database scanners, attached read-only before the
      session is locked; Phase 03's "external access disabled" still holds for
      everything else.
- [ ] Row and time limits pushed down to the source, so a large table is never copied
      whole.

**Workstream C: Hosted example page**
- [ ] The Phase 08 Streamlit page can be deployed as a public example. There, uploads
      are stored in the page's database, in open mode, with a stated retention period
      and a delete control.
- [ ] Visitors use their own model key, held for the session only and never stored.
- [ ] The page states, before upload, what is stored, for how long, and what is sent to
      the model provider.

**Experiment (paid, ranked with the other paid uses)**
- [ ] Sealed against open on the tuning cases and the field set, with the Phase 06
      paired statistics, so the accuracy cost of sealed mode is a measured number in
      the docs.

**Tests**
- [ ] Canary test: a fixture plants unique values in rows, log text and documents; a
      sealed run with the fake client captures every outgoing message, and none of
      them contains a canary.
- [ ] Masking is stable across tool calls and reversible locally.
- [ ] A citation quoting a masked token passes; one quoting the hidden value fails.
- [ ] Bring-your-own-database against a SQLite fixture; writes are refused.
- [ ] Update `flowchart-system-overview.svg` for the masking layer and the database
      input.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: Sealed is the default for local installs

* **Decision:** A downloaded or cloned copy starts sealed; open mode is a setting the
  user turns on.
* **Context/Constraint:** The people most likely to need the tool are the ones whose
  files they cannot share freely.
* **Trade-off:** The default may be less accurate than open mode; the experiment above
  states by how much.

#### Decision 2: Masking happens in the tool layer, not in the prompt

* **Decision:** Tool results are masked before they enter the conversation; the agent
  code never holds unmasked values in model-bound text.
* **Context/Constraint:** A prompt instruction not to repeat values is not a control.
* **Trade-off:** Tools carry a mode parameter, and each tool's sealed output needs its
  own tests.

#### Decision 3: The hosted example is the only stored-upload path

* **Decision:** Stored uploads exist only on the hosted example page, which is separate
  from the local install.
* **Context/Constraint:** Phase 08 Decision 3 (local only, no authentication) still
  holds for the product; the hosted page is a demonstration.
* **Trade-off:** The hosted page needs retention, deletion and abuse limits that the
  local tool does not.

## 4. Constraints

* **Users bring their own model keys.** *Source: owner.* **Implication:** no key is
  stored by the tool or by the hosted page.
* **Sealed by default for downloaded copies; the hosted example uses its database.**
  *Source: owner.*
* **⚠️ OPEN:** whether "sealed" should also restrict what the model sees (as planned
  above) or only where data is stored. The plan assumes both.
* **⚠️ OPEN:** where the hosted example runs and its retention period.

## 5. Verification evidence
*Filled in as the work happens.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
