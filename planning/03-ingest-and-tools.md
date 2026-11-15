# Phase 3: Ingest & Tools

**Status:** Not started

## 1. Objective
Turn any folder of dropped files into a read-only database with a catalog — tables for
data and logs, searchable chunks for documents — and give the agent a small set of
generic, well-tested tools for querying it. Ends with tools that
work the same on an RCAEval case and on an arbitrary upload, with no LLM involved yet.

## 2. Next Steps (Checklist)

**Workstream A — Ingest (`src/incidents_agent/ingest/`)**
- [ ] Loader for CSV, Parquet and JSON lines: one DuckDB table per file. Table names
      are derived from the relative path and made unique, so two files with the same
      name in different folders cannot collide; the catalog maps each table to its file.
- [ ] Loader for text logs: table with line number, raw text, and timestamp and level
      where detected.
- [ ] Time-column detection (name hints, then value parsing) with a confidence score.
      The original column is kept unchanged; a normalised Unix-seconds column is added
      beside it, and the inferred unit and time zone assumption are recorded in the
      catalog.
- [ ] Tables with a low-confidence or missing time mapping are flagged. Time-window
      tools (`compare_windows`, `time_series`, `find_onset`, the before/after counts in
      `search_text`) return an explicit error for such a table, not a guess.
- [ ] File classifier: tabular, log or document, decided by extension and then by
      content (share of lines that start with a timestamp or level); the decision and
      its reason are recorded in the catalog.
- [ ] Document loader: Markdown and prose text chunked by heading, then paragraph, with
      the heading path kept on each chunk; diffs chunked by file and hunk; config files
      (YAML, TOML, INI, JSON) chunked by top-level section; small documents kept whole.
      Oversized chunks are split at sentence boundaries with a small overlap.
- [ ] *(After Phase 04 — needs the model client, spend guard and fake.)*
      Model-assisted fallback for files the rules cannot place with confidence: one
      cheap model call sees the file name and a short sample and picks from a fixed
      menu — file kind (tabular, log, document) and chunking strategy (headings,
      paragraphs, hunks, sections, whole). The choice, its stated reason and its cost
      are recorded in the catalog. Off by a configuration flag; cached by file hash;
      reserved through the spend guard; never used when the rules are confident.
      Deterministic ingest lands first and works without it.
- [ ] Chunks stored in a `document_chunks` table: document, chunk id, position, heading
      path, text.
- [ ] Catalog builder: per table, the columns, types, row count, time column, time range
      and three sample rows; per document, its title, type, size and outline.
- [ ] Limits, all configurable and all reported when hit: size per file, number of
      files, total size, rows scanned per query, query time, and result size.
      Unsupported or unreadable files are recorded as skipped with the reason; ingest of
      the remaining files continues.
- [ ] Supported inputs and partial-ingest behaviour written up in `docs/inputs.md`.
- [ ] Open the database read-only with external file and network access disabled.

**Workstream B — Tools (`src/incidents_agent/tools/`)**
- [ ] Tool registry: each tool is a typed Python function plus a JSON schema; a single
      `call(name, args)` entry point returns a JSON-serialisable result.
- [ ] Result cap of about 6,000 characters with an explicit truncation notice.
- [ ] Errors returned as `{"error": ...}` data, never raised to the agent loop.
- [ ] `list_tables`, `describe_table`.
- [ ] `run_sql`: read-only, row limit, timeout, rejects anything that is not a `SELECT`.
- [ ] `search_text`: regex over a text column, grouped by message pattern (numbers and
      ids masked), with counts before and after a time.
- [ ] `compare_windows`: rank numeric columns by a robust shift score across a split time.
- [ ] `time_series`: one column downsampled into at most N buckets.
- [ ] `search_documents`: keyword (BM25) search over document chunks; returns chunk
      ids, heading paths and short excerpts.
- [ ] `read_document`: one chunk by id with its neighbouring chunks, or a document's
      outline.
- [ ] Document chunks are citable: a chunk read through a tool counts as evidence in
      the ledger like any other tool result.
- [ ] `find_onset`: estimate when the incident started from change points in numeric
      columns and in error-line counts, for inputs with no known time.
- [ ] Tool-call log: every call recorded with an id, arguments, result size and duration.
- [ ] Tool results carry a stable locator for what they returned: table and row keys
      or aggregate expression and time window for queries, chunk ids for documents. The
      ledger cites these (Phase 05).
- [ ] Untrusted text at the tool boundary: file content is returned inside a marked
      data field, never as free text mixed with instructions.
- [ ] Best-effort secret redaction on every tool result before it reaches a model or a
      saved trajectory: common key, token and password patterns are masked and the count
      is reported. Documented as pattern-based, not a guarantee.

**Both**
- [ ] Unit tests for every loader and tool, including malformed input and empty tables.
- [ ] Fallback tests with the fake client: called only below the confidence threshold;
      an answer outside the menu is rejected and the rule-based default used; a cached
      file is not asked about twice; with the flag off, no model call is made.
- [ ] Chunking tests: heading paths, hunk boundaries, the whole-document threshold,
      oversized paragraphs, a document with no headings, and a log misnamed `.txt`.
- [ ] Build two field-set cases now, as fixtures under `evalsets/field/`: the
      config-diff case and the victim-versus-cause case. They exercise documents, the
      catalog and cross-source reasoning from the start; Phase 06 adds the other four.
- [ ] Integration test: ingest both fixture cases and run every tool against them.
- [ ] Integration test: a folder with no usable timestamps ingests cleanly, is flagged,
      and time-window tools refuse it with a clear error.
- [ ] Tests for name collisions, each limit, and redaction of planted fake secrets.
- [ ] Integration test (skipped when the dataset is absent): ingest one real RE2-OB case.
- [ ] Update `flowchart-system-overview.svg` if the tool list changed.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: DuckDB as the single store for dropped files

* **Decision:** Every run loads its files into one DuckDB database and all tools read
  from it.
* **Context/Constraint:** Inputs are unknown in advance. DuckDB reads CSV, Parquet and
  JSON natively, handles wide metric tables, needs no server, and gives the agent one
  query language for everything.
* **Trade-off:** Text logs fit a relational table awkwardly (one row per line), and
  large uploads are limited by local memory and disk.

#### Decision 2: Generic tools, not RCAEval-shaped ones

* **Decision:** Tools know about tables, columns and time, not about services, metrics
  or traces.
* **Context/Constraint:** The end product is a drop-files platform. Tools that assume
  RCAEval's column naming would score better on the benchmark and fail on anything else.
* **Trade-off:** The agent spends tool calls discovering structure that a purpose-built
  tool would know. If this costs accuracy, the report states by how much.
* **Rejected alternatives:** RCAEval's `CaseToolkit`. Its `rank_anomalies` tool is BARO
  itself, so an agent built on it mostly measures BARO, and it cannot open an arbitrary
  upload. Its reference agent stays useful as a baseline.

#### Decision 3: `compare_windows` is a statistic, not an answer

* **Decision:** The tool returns ranked column shifts with before and after values; it
  does not name a root cause.
* **Context/Constraint:** It is the same family of statistic as BARO. The agent's job is
  to cross-check it against logs and traces and to separate cause from symptom.
* **Trade-off:** The agent can still simply trust the top row. The ablation in Phase 06
  shows whether the ledger and verify steps change that.

#### Decision 4: Documents are chunked by structure and searched by keyword

* **Decision:** Prose, diffs and config files are split along their own structure
  (headings and paragraphs, hunks, sections) into chunks stored in the same database,
  searched with BM25, and read with their neighbours.
* **Context/Constraint:** Supporting material — a runbook, a config diff, an
  architecture note — is only useful if the agent can find the relevant part and cite
  it. One row per line destroys meaning; one chunk per file cannot be cited precisely
  and may not fit in a tool result.
* **Trade-off:** Structure-based chunking depends on the document having structure; a
  wall of text falls back to paragraph and sentence splits. Keyword search misses
  paraphrases; it is the same choice, for the same reasons, as in the incident memory,
  and can be replaced by embeddings behind the same tool.
* **Rejected alternatives:** fixed-size chunks with overlap. Simple and format-blind,
  but they cut through hunks and sections, so a cited chunk can start mid-sentence and
  lose the heading that says what it is about.

#### Decision 5: Rules choose the chunking; a model is the fallback, from a fixed menu

* **Decision:** File kind and chunking strategy are decided by deterministic rules.
  Only when the rules are unsure does one small model call choose, and it can only pick
  among the strategies the code already implements.
* **Context/Constraint:** Most files announce what they are: an extension, a header
  row, lines starting with timestamps, `@@` hunk markers, Markdown headings. Paying a
  model for those adds cost and variance for nothing. The hard cases are real, though:
  a `.txt` that could be a log or a note, or prose with no headings.
* **Trade-off:** Ingest is no longer fully deterministic for ambiguous files, so the
  choice is cached by file hash and recorded, and the upload page lets the user
  override it. A wrong pick costs retrieval quality, not correctness of the data.
* **Rejected alternatives:** a model step on every file, or a model that proposes
  chunk boundaries directly. The first spends budget where rules are already right and
  makes every evaluation run depend on an extra model call. The second reads the whole
  document through the model, which is expensive for long files and impossible to
  unit-test.

#### Decision 6: Read-only SQL with a hard sandbox

* **Decision:** `run_sql` accepts single `SELECT` statements only, on a read-only
  connection with external access disabled, with a row limit and a timeout.
* **Context/Constraint:** The SQL is written by a model reading untrusted log text.
* **Trade-off:** No temporary tables or multi-statement scripts, so some analyses take
  more calls.

## 4. Constraints

* **Drop-files platform.** *Source: owner.* **Implication:** ingest must degrade
  gracefully; an odd file is skipped and reported, never fatal.
* **Supporting documents are first-class input.** *Source: owner.* **Implication:** the
  classifier must not load a runbook as a log or a log as a document; both mistakes are
  tested.
* **Result size.** *Source: budget.* **Implication:** every tool result is capped,
  because tool output is the largest share of token spend.
* **A wrong time mapping corrupts every before/after comparison silently.** *Source:
  plan review.* **Implication:** time handling fails loudly; a guess is never used
  without being shown.

## 5. Dataset notes
*Actual columns of `metrics.csv`, `logs.csv` and `traces.csv`, recorded in Phase 01.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
