# Supported Inputs and Ingest Behaviour

`incidents_agent.ingest` turns any folder of dropped files into a read-only
DuckDB database (`incident.duckdb`) and a structured catalog describing every
table, detected time mapping, relationship, document outline, classification
decision, and skipped file.

## 1. File Classification

Files are classified deterministically by extension and bounded content
inspection (`IngestConfig`). A user override (`classification_overrides`) takes
precedence when supplied.

| File kind | Extensions / content signals | Storage target | Chunk strategy |
|---|---|---|---|
| **Tabular** | `.csv`, `.tsv`, `.parquet`, `.jsonl`, `.ndjson`, `.json` (top-level array) | One DuckDB table per file | `none` |
| **Log** | `.log`, or `.txt`/text files where >= 25% of sampled lines begin with a timestamp or log level | One DuckDB table `(line_no, raw_text, timestamp, level)` | `none` |
| **Document** | `.md`, `.markdown`, `.rst`, `.diff`, `.patch`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.conf`, `.json` (object), or prose `.txt` | `document_chunks` table | `headings`, `paragraphs`, `hunks`, `sections`, or `whole` |

Table names are derived from each file's relative path (for example,
`service_a/metrics.csv` becomes `service_a__metrics`), normalised to safe SQL
identifiers, and disambiguated with numeric suffixes if collisions still occur.

### Optional Model-Assisted Fallback

When `enable_model_classification` is enabled, `classifier_client` is supplied
(a Phase 04 `ModelClient`, such as `GuardedClient` or `ScriptedClient`), and
the deterministic rules fall below `classifier_confidence_threshold` (default
`0.70`), a single fallback call may choose a `(kind, chunk_strategy)` pair from
the fixed menu above. The request passes only `relative_path` and a bounded
`untrusted_sample` (up to 2,000 bytes) inside JSON data, while the system prompt
treats the sample strictly as untrusted data.

- **Spend guard and accounting:** Calls go through `ModelClient.complete(...)`
  using `classifier_run` and `classifier_case_id` (defaulting to the folder
  name during `ingest_folder`), reserving and settling against `SpendLedger`
  with exact `Decimal` cost recorded in `FileClassification.model_cost_usd`.
- **Caching and validation:** Valid menu choices are cached by SHA-256 file
  hash (`classifier_cache`) and reused at zero cost. Malformed or out-of-menu
  responses are rejected in favour of the deterministic default without caching,
  while retaining any settled call cost.
- **Graceful degradation:** `BudgetRefused`, `BillingUncertain`,
  `ModelUnavailable`, `ProviderError`, and `RetryDeferred` fall back to the
  deterministic default. `BudgetRefused`, `BillingUncertain`, and
  `ModelUnavailable` also stop further model fallback calls for the remaining
  files in that `ingest_folder` run.

## 2. Time Detection and Normalisation

Every tabular and log table is scanned for timestamp columns:

1. **Candidate scoring** combines column name hints (`timestamp`, `time`,
   `startTimeMillis`, `startTime`, `created_at`, etc.) with sampled value
   parsing (Unix seconds, milliseconds, microseconds, nanoseconds, and ISO-8601
   timestamps).
2. **Normalisation** preserves the original column unchanged and adds a
   `_time_unix_s` (`DOUBLE`) column alongside it, recording the inferred unit,
   timezone assumption (naive timestamps assume UTC), confidence, and time range
   in the catalog.
3. **Explicit failure on unusable time**: clock-only strings without a date
   (such as `HH:MM`), missing time columns, or scores below
   `time_confidence_threshold` mark `time_usable = False`. No `_time_unix_s`
   column is guessed, and time-window tools return an explicit error for that
   table.

## 3. Document Chunking

Supporting documents are chunked along their own structure into
`document_chunks(doc_id, chunk_id, position, heading_path, text)`:

- **Small documents** at or below `whole_doc_max_chars` (default `1,200`) are
  kept whole unless they exceed `max_chunk_chars`.
- **Markdown and prose** split by heading hierarchy (`heading_path` tracks
  `H1 > H2 > ...`), then by paragraph when sections are long; plain prose without
  headings splits by paragraph.
- **Diffs** (`.diff`, `.patch`, or unified diff markers) split by file and
  `@@ ... @@` hunk.
- **Config files** (JSON objects, YAML, TOML, INI) split by top-level section.
- **Oversized chunks** above `max_chunk_chars` (default `1,500`) split at
  sentence boundaries with `chunk_overlap_chars` (default `150`) of overlap.

## 4. Relationship Detection

Tables are inspected for component relationship columns:

- **Direct caller/callee pairs**: `(caller, callee)`, `(source, destination)`,
  `(src, dst)`, `(client, server)`, `(from_service, to_service)`, `(upstream,
  downstream)`.
- **Span hierarchies**: `(spanID, parentSpanID)` paired with a service/component
  column (`serviceName`, `service`, `component`, `container_name`) and verified
  by parent-to-span value overlap.

Optional duration and status/error columns are recorded alongside the
relationship. Tables with neither pattern are explicitly recorded with empty
relationships.

## 5. Limits and Partial-Ingest Behaviour

All limits live in `IngestLimits` and default to:

| Limit | Default |
|---|---|
| `max_file_bytes` | 100 MiB (accommodates RE2-OB `traces.csv`) |
| `max_files` | 50 files |
| `max_total_bytes` | 250 MiB |
| `max_query_rows` | 500 rows |
| `max_scan_rows` | 2,000,000 rows |
| `query_timeout_s` | 5.0 seconds |
| `max_result_chars` | 6,000 characters |

Ingest degrades gracefully per file:

- Binary, non-UTF-8, corrupt, unsupported, or oversized files—and files beyond
  `max_files` or `max_total_bytes`—are recorded in `catalog.skipped_files` with
  their reason, and any hit limit is listed in `catalog.limits_hit`.
- Ingest continues for all remaining valid files in the folder.

## 6. Read-Only Sandbox

Once loading completes, the writable connection is closed and the database is
re-opened in `read_only=True` mode with `enable_external_access = false` and
`lock_configuration = true`, preventing writes as well as local file or network
access through SQL.

## 7. Tool Boundary, SQL Sandbox, and Redaction

All investigation tools (`incidents_agent.tools`) read exclusively through the
sandboxed connection and registry:

- **Read-only SQL (`run_sql`):** Accepts only single `SELECT` (or `WITH ...
  SELECT`) statements, strips comments outside string literals, blocks DDL/DML,
  multi-statements, `PRAGMA`/`ATTACH`/`COPY`/`SET`, and file-reading table
  functions, and enforces `max_query_rows` and `query_timeout_s`.
- **Untrusted data boundary:** Content extracted from files, tables, logs, and
  document chunks is returned exclusively inside `untrusted_data.content`,
  separated from structural metadata, locators, and agent instructions.
- **Result size cap:** Serialised tool payloads are truncated deterministically
  to `max_result_chars` (default `6,000` characters) with `truncated=True`, an
  explicit `truncation_notice`, and a `truncation` record listing each trimmed
  list with the items kept and the total. Raw data in `untrusted_data` is
  trimmed first; `summary`, `locator`, `error` and `target_components` are never
  trimmed, so counts in `summary` describe the result before the cap.
- **Best-effort secret redaction:** All string values returned by tools pass
  through regex masking for common API keys, AWS/GCP/GitHub/Slack tokens,
  Bearer headers, JWTs, PEM private keys, and key-value passwords/secrets,
  replacing matches with `[REDACTED_SECRET]` and reporting
  `redacted_secrets_count`. This is pattern-based best effort, not a guarantee.

