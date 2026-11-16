# Phase 3: Ingest & Tools

**Status:** Ready for review

## 1. Objective
Turn any folder of dropped files into a read-only database with a catalog — tables for
data and logs, searchable chunks for documents — and give the agent a small set of
generic, well-tested tools for querying it. Ends with tools that
work the same on an RCAEval case and on an arbitrary upload, with no LLM involved yet.

## 2. Next Steps (Checklist)

**Workstream A — Ingest (`src/incidents_agent/ingest/`)**
- [x] Loader for CSV, Parquet and JSON lines: one DuckDB table per file. Table names
      are derived from the relative path and made unique, so two files with the same
      name in different folders cannot collide; the catalog maps each table to its file.
- [x] Loader for text logs: table with line number, raw text, and timestamp and level
      where detected.
- [x] Time-column detection (name hints, then value parsing) with a confidence score.
      The original column is kept unchanged; a normalised Unix-seconds column is added
      beside it, and the inferred unit and time zone assumption are recorded in the
      catalog.
- [x] Tables with a low-confidence or missing time mapping are flagged. Time-window
      tools (`compare_windows`, `time_series`, `find_onset`, the before/after counts in
      `search_text`) return an explicit error for such a table, not a guess.
- [x] File classifier: tabular, log or document, decided by extension and then by
      content (share of lines that start with a timestamp or level); the decision and
      its reason are recorded in the catalog.
- [x] Document loader: Markdown and prose text chunked by heading, then paragraph, with
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
      Deterministic ingest lands first and works without it. *(Protocol hook, hash
      cache, menu validation, and fake-client unit tests landed in Workstream A;
      Phase 04 wires the real LiteLLM client and spend guard.)*
- [x] Chunks stored in a `document_chunks` table: document, chunk id, position, heading
      path, text.
- [x] Catalog builder: per table, the columns, types, row count, time column, time range
      and three sample rows; per document, its title, type, size and outline.
- [x] Relationship detection: columns that describe who talks to whom — caller and
      callee, source and destination, or a span id with a parent span id — are
      identified by name and by value overlap, and recorded in the catalog with a
      confidence. Tables with none are recorded as having none.
- [x] Limits, all configurable and all reported when hit: size per file, number of
      files, total size, rows scanned per query, query time, and result size.
      Unsupported or unreadable files are recorded as skipped with the reason; ingest of
      the remaining files continues.
- [x] Supported inputs and partial-ingest behaviour written up in `docs/inputs.md`.
- [x] Open the database read-only with external file and network access disabled.

**Workstream B — Tools (`src/incidents_agent/tools/`)**
- [x] Tool registry: each tool is a typed Python function plus a JSON schema; a single
      `call(name, args)` entry point returns a JSON-serialisable result.
- [x] Result cap of about 6,000 characters with an explicit truncation notice.
- [x] Errors returned as `{"error": ...}` data, never raised to the agent loop.
- [x] `list_tables`, `describe_table`.
- [x] `run_sql`: read-only, row limit, timeout, rejects anything that is not a `SELECT`.
- [x] `search_text`: regex over a text column, grouped by message pattern (numbers and
      ids masked), with counts before and after a time.
- [x] `compare_windows`: rank numeric columns by a robust shift score across a split time.
- [x] `time_series`: one column downsampled into at most N buckets.
- [x] `search_documents`: keyword (BM25) search over document chunks; returns chunk
      ids, heading paths and short excerpts.
- [x] `read_document`: one chunk by id with its neighbouring chunks, or a document's
      outline.
- [x] Document chunks are citable: a chunk read through a tool counts as evidence in
      the ledger like any other tool result. *(Tools attach `document_chunks` locators
      with `chunk_ids`; Phase 05 enforces ledger citation validation.)*
- [x] `find_onset`: estimate when the incident started from change points in numeric
      columns and in error-line counts, for inputs with no known time. Its accuracy is
      measured against RCAEval's known start times in Phase 06, at no cost.
- [x] `dependency_graph`: when the catalog has relationship columns, return the edges
      between components with call counts, and error rate and latency before and after
      the incident time per edge; optionally only the neighbourhood of one component.
      When there is no relationship data it says so explicitly, so the agent knows
      direction cannot be established from the files.
- [x] Tool-call log: every call recorded with an id, arguments, result size and duration.
- [x] Tool results carry a stable locator for what they returned: table and row keys
      or aggregate expression and time window for queries, chunk ids for documents. The
      ledger cites these (Phase 05).
- [x] Untrusted text at the tool boundary: file content is returned inside a marked
      data field, never as free text mixed with instructions.
- [x] Best-effort secret redaction on every tool result before it reaches a model or a
      saved trajectory: common key, token and password patterns are masked and the count
      is reported. Documented as pattern-based, not a guarantee.

**Both**
- [x] Unit tests for every loader and tool, including malformed input and empty tables.
- [x] Fallback tests with the fake client: called only below the confidence threshold;
      an answer outside the menu is rejected and the rule-based default used; a cached
      file is not asked about twice; with the flag off, no model call is made.
- [x] Chunking tests: heading paths, hunk boundaries, the whole-document threshold,
      oversized paragraphs, a document with no headings, and a log misnamed `.txt`.
- [x] Build two field-set cases now, as fixtures under `evalsets/field/`: the
      config-diff case and the victim-versus-cause case. They exercise documents, the
      catalog and cross-source reasoning from the start; Phase 06 adds the other four.
- [x] Integration test: ingest both fixture cases and run every tool against them.
- [x] Integration test: a folder with no usable timestamps ingests cleanly, is flagged,
      and time-window tools refuse it with a clear error.
- [x] Tests for name collisions, each limit, and redaction of planted fake secrets.
- [x] Dependency tests: caller/callee columns, span and parent-span ids, a table with
      no relationship data, and a cycle.
- [x] Injection plumbing test: a log line and a document paragraph containing
      instructions addressed to the agent come back only inside the marked data field,
      byte for byte, never in any other part of a tool result.
- [x] Integration test (skipped when the dataset is absent): ingest one real RE2-OB case.
- [x] Update `flowchart-system-overview.svg` if the tool list changed. *(Verified
      `flowchart-system-overview.svg` summarizes the four tool categories accurately.)*

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

#### Decision 7: A dependency tool, generic over column roles

* **Decision:** One tool builds a component graph from whatever relationship columns
  the catalog found, and reports how each edge changed across the incident time.
* **Context/Constraint:** Telling a root cause from a victim needs direction: who calls
  whom. With only SQL, the agent has to rediscover the topology from raw rows in every
  run, spending tool calls it needs elsewhere, and often does not. RCAEval's own
  toolkit ships a call-graph tool for the same reason.
* **Trade-off:** This is the most benchmark-friendly tool in the set, since RCAEval's
  traces fit it exactly, so it weakens the "nothing is shaped to the benchmark" claim.
  It is kept generic (roles are detected, not assumed), it returns nothing when the
  data has no relationships, and its effect is measurable by switching it off.

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
Phase 01 read only the CSV headers of the first three blind RE2-OB cases
in manifest order. The 71 shared `metrics.csv` columns occurred in the
same order in all three cases.

    time, adservice_cpu, cartservice_cpu, checkoutservice_cpu,
    currencyservice_cpu, emailservice_cpu, frontend_cpu, paymentservice_cpu,
    productcatalogservice_cpu, recommendationservice_cpu, redis_cpu,
    shippingservice_cpu, adservice_mem, cartservice_mem, checkoutservice_mem,
    currencyservice_mem, emailservice_mem, frontend_mem, paymentservice_mem,
    productcatalogservice_mem, recommendationservice_mem, redis_mem,
    shippingservice_mem, adservice_diskio, emailservice_diskio,
    recommendationservice_diskio, redis_diskio, adservice_socket,
    cartservice_socket, checkoutservice_socket, currencyservice_socket,
    emailservice_socket, frontend_socket, paymentservice_socket,
    productcatalogservice_socket, recommendationservice_socket, redis_socket,
    shippingservice_socket, adservice_workload, cartservice_workload,
    checkoutservice_workload, currencyservice_workload, emailservice_workload,
    frontend_workload, frontend-external_workload, paymentservice_workload,
    productcatalogservice_workload, recommendationservice_workload,
    shippingservice_workload, frontend_error, frontend-external_error,
    adservice_latency-50, cartservice_latency-50, checkoutservice_latency-50,
    currencyservice_latency-50, emailservice_latency-50, frontend_latency-50,
    paymentservice_latency-50, productcatalogservice_latency-50,
    recommendationservice_latency-50, shippingservice_latency-50,
    adservice_latency-90, cartservice_latency-90, checkoutservice_latency-90,
    currencyservice_latency-90, emailservice_latency-90, frontend_latency-90,
    paymentservice_latency-90, productcatalogservice_latency-90,
    recommendationservice_latency-90, shippingservice_latency-90

| Blind case | Metric columns | Additional columns |
|---|---:|---|
| `re2-ob-0001` | 77 | `cartservice_diskio`, `currencyservice_diskio`, `frontend_diskio`, `paymentservice_diskio`, `productcatalogservice_diskio`, `adservice_error` |
| `re2-ob-0002` | 75 | `cartservice_diskio`, `currencyservice_diskio`, `frontend_diskio`, `productcatalogservice_diskio` |
| `re2-ob-0003` | 72 | `adservice_error` |

`logs.csv` and `traces.csv` had identical headers across the three cases.

- `logs.csv`

    time, timestamp, container_name, message, level, req_path, error

- `traces.csv`

    time, traceID, spanID, serviceName, methodName, operationName,
    startTimeMillis, startTime, duration, statusCode, parentSpanID

- Sampled values across the three cases show Unix seconds in metrics `time`.
  Logs and traces use a clock string (`HH:MM`) in `time`. Logs provide Unix
  nanoseconds in `timestamp`. Traces provide Unix milliseconds in
  `startTimeMillis` and microseconds in `startTime`.
- Each sampled blind case contains nine files and totals 72–98 MB. Traces
  account for 50–74 MB and logs for 13–16 MB. Other files include raw metrics,
  derived log and trace series, log templates, and task metadata. Use these
  sizes when verifying ingest limits and memory use.
- Raw metric headers contain `*_latency-50` and `*_latency-90`. RCAEval
  excludes the former from `task.json` candidate indicators and exposes the
  latter as `*_latency`. Preserve raw evidence names when mapping predictions.
- The sampled traces contain `serviceName=frontendservice`, while candidate
  services include `frontend` and the log container field can also hold
  `frontend`. Derived trace series include `frontendservice_frontend`. Verify
  aliases before joining or filtering across sources.

## 6. AI-use notes (feeds `REPORT.md`)
- Workstream A (`src/incidents_agent/ingest/`, `docs/inputs.md`, `tests/ingest/`):
  implemented deterministic file classification, time-column scoring and `_time_unix_s`
  normalisation (normalising DuckDB `TIMESTAMPTZ` columns to UTC `TIMESTAMP` on load so
  queries do not require `pytz`), structure-aware document chunking, relationship
  detection (`caller_callee` and `span_parent`), configurable limits, and read-only
  DuckDB connections (`enable_external_access = false`, `lock_configuration = true`).
  Verified with `make lint` and `make test` (54 tests passing, 91% line coverage on
  `ingest`, including real `re2-ob-0001` case ingest).
- Workstream B (`src/incidents_agent/tools/`, `evalsets/field/`, `tests/tools/`):
  implemented the 10 generic investigation tools (`list_tables`, `describe_table`,
  `run_sql`, `search_text`, `compare_windows`, `time_series`, `find_onset`,
  `search_documents`, `read_document`, `dependency_graph`), the typed registry with
  call logging, `untrusted_data` envelope, pattern-based secret redaction, 6,000-char
  truncation cap, and the seeded `config_diff` and `victim_vs_cause` field fixtures
  (with gold labels kept outside ingested case folders). Verified with `make lint`
  and `make test` (106 passed including real `re2-ob-0001`, 92% overall line coverage).
