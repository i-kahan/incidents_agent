# Phase 2: Baseline & Headroom Study

**Status:** Merged — split in #2; baselines and headroom in #6

## 1. Objective
Fix the tuning and held-out split first, then measure what the statistical baselines
already achieve on RE2-OB and where they fail, and therefore how much room an agent has
to add value. Ends with a locked split, baseline numbers on disk, and a benchmark target
confirmed or changed by a rule written down before the numbers were seen.

## 2. Next Steps (Checklist)

**Split — done first, before any labelled result is looked at**
- [x] Group the 90 cases by service–fault pair (30 pairs, three repetitions each) using
      the answers file. The grouping is done by a script; no per-case label is printed.
- [x] Create `splits/re2-ob.json`: 10 pairs (30 cases) for tuning, 20 pairs (60 cases)
      held out, from a fixed seed, constrained so that every fault type and every
      service appears at least once in tuning. All three repetitions of a pair are on
      the same side.
- [x] Write the matching answers subsets (`tuning.answers.jsonl`,
      `heldout.answers.jsonl`) beside the full answers file, outside the task directory.
- [x] Commit the split file and its seed. It does not change after this point.
- [x] Unit tests for the split: deterministic, disjoint, sizes, no pair on both sides,
      coverage constraint met.

**Baselines — free**
- [x] Run BARO on all 90 cases; save `predictions.jsonl`. RCAEval 1.12.0 ships no BARO
      example script, so `make baselines` calls its `baro()` directly (Decision 6).
- [x] Score tuning and held-out separately, each against its own answers subset, with
      `--report-chance`. For held-out cases keep **aggregate scores only**: no per-case
      output is written or read.
- [x] If it runs in the pinned setup, also run multi-source BARO (`mmbaro`), which uses
      metrics, logs and traces. It separates "the agent reasons better" from "the agent
      sees more data".

**Headroom analysis — tuning cases only**
- [x] Break BARO's tuning scores down by fault type and by service; list its misses at
      top 1 and at top 3.
- [x] For up to ten tuning misses, check by hand whether logs or traces point to the
      true root cause. Record the count.
- [x] Estimate cost per case: tokens in a typical case's tool outputs, times the tool
      call limit, at the configured models' prices. Compare with the cost per case the
      final-run allocation implies — $0.05 at the default limit (Phase 06, Decision 6).
- [x] Apply the target rule (Decision 3) and record the outcome.
- [x] Unit tests for the breakdown code.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: The baseline is measured before the agent is designed in detail

* **Decision:** Baseline results and failure cases are produced first and used to steer
  the agent's prompt and tools.
* **Context/Constraint:** BARO scores 0.74 top-1 and 0.98 top-5 at service level on
  RE1-OB per RCAEval's docs. If RE2-OB is similar, an agent can only add value at top 1
  and at indicator level; knowing that changes what is worth building.
* **Trade-off:** Time is spent on measurement before any agent code exists.

#### Decision 2: The split is locked first, and grouped by service–fault pair

* **Decision:** The split is created before any baseline result is examined. Cases are
  split by service–fault pair, 10 pairs for tuning and 20 held out, so no held-out
  scenario has a repetition in the tuning set. Held-out cases are seen only as
  aggregate scores until the final run.
* **Context/Constraint:** RE2 has three repetitions of every service–fault pair. A
  random split would put repetitions of the same scenario on both sides, so held-out
  results would measure recognising a scenario already tuned on. It would also make the
  memory experiment a lookup of known answers. Inspecting failures before splitting
  would let held-out cases shape prompts, tools and the choice of benchmark.
* **Trade-off:** Labels are used to form the groups, though only by a script. Tuning
  covers 10 scenarios, not 30 independent cases, so its intervals are wider than a
  30-case count suggests; all intervals are computed by resampling pairs, not cases.
  Tuning cannot contain every service–fault combination, so some held-out scenarios are
  of a kind never tuned on, which is the point.
* **Rejected alternatives:** a random 30 / 60 split by case. Simpler and label-free,
  but with three repetitions per scenario it leaks, and it overstates the precision of
  every interval.

#### Decision 3: The target is changed only by a rule fixed in advance

* **Decision:** The target stays RE2-OB as exported unless BARO's top-1 service accuracy
  on the tuning set is 0.90 or higher. In that case the agent is given no fault start
  time and must find it; the baselines are re-run under the same condition, and the
  changed protocol is named as such in every table.
* **Context/Constraint:** Choosing the benchmark after seeing where the agent could win
  is a form of tuning. A threshold set beforehand removes the choice.
* **Trade-off:** The threshold is a judgment call made without data, and the tuning set
  is small enough that the rule can fire, or fail to, by chance.

#### Decision 4: The committed split names groups, never labels

* **Decision:** `splits/re2-ob.json` lists case ids per opaque group (`g01`–`g30`,
  numbered by each group's smallest case id), the side of each group, the seed, the
  number of draws the seed needed, and the SHA-256 of the answers file. It records no
  service and no fault. `make split` creates the file once; after that it only
  verifies it (same answers digest, same result when rebuilt from the seed) and
  refuses to continue on a mismatch. It never rewrites the file.
* **Context/Constraint:** Phase 06 bootstraps intervals by pair, including on held-out
  cases whose labels must stay unread until the final run. The group ids let it
  resample by pair without opening the answers. The PR checklist forbids benchmark
  answers in the diff.
* **Trade-off:** Anyone reading the file learns which cases share a scenario, though
  not what the scenario is. The agent is given only the task view, never `splits/`.
* **Rejected alternatives:** recomputing the pairs from the answers whenever they are
  needed. It reads held-out labels to do it, and it lets a changed export silently
  change the split.

#### Decision 5: Tuning is drawn whole, and redrawn until coverage holds

* **Decision:** Each draw takes 10 of the 30 groups uniformly at random, from one
  generator seeded with 2026. The first draw that covers every service and every fault
  type is kept (the seed needed two draws).
* **Context/Constraint:** Decision 2 requires full coverage in tuning; fixing the seed
  in code before any result was read keeps the choice out of the owner's hands.
* **Trade-off:** Rejection sampling conditions the draw on coverage, so the split is
  uniform only over the covering draws. A stratified draw would be harder to explain.

#### Decision 6: BARO is run as RCAEval ships it, with its full ranking

* **Decision:** BARO is RCAEval's `baro()` with its default preprocessing, called on
  the toolkit's standard window. The full ranking is kept. The toolkit's
  `rank_anomalies` stops at 20 entries, which can leave fewer than five services.
* **Context/Constraint:** Decision 3 names "BARO's top-1" as the reference, and RCAEval
  ships no BARO script for the agent task view. Calling the function directly is the
  closest thing to the published method.
* **Trade-off:** On RE2-OB this BARO ranks `redis_diskio` first in every tuning case
  (§5). That series is zero most of the time with periodic bursts. Its interquartile
  range is zero, so its robust z-score is huge. The baseline is therefore weak at top 1
  for a reason unrelated to the fault. The report states this. Phase 06 adds a
  guarded BARO row that skips zero-IQR series (owner's decision, §5).
* **Rejected alternatives:** RCAEval's own `dk_select_useful` option. It drops the
  `redis_*` series, but its variance filter leaves one or two metrics per case on
  RE2-OB. It scored 0.20 at top 1, 3 and 5 on tuning, so it is not a usable baseline.

#### Decision 7: Multi-source BARO uses the same window, and is also scored on candidates only

* **Decision:** `mmbaro` gets metrics, log-template counts and both trace series, each
  cut to the case's standard window and NaN-filled as RCAEval's toolkit does. It is
  scored twice: as it ranks, and with names whose service is not a candidate removed.
  Of its names, 271 map to non-candidate services: `frontendservice`, `traceservice` and
  `health` from trace operations.
* **Context/Constraint:** RCAEval 1.12.0 has `mmbaro()` but no loader for the agent
  task view. The agent must answer from the closed candidate list, so the filtered
  row is the fairer comparison. The raw row shows the method as published.
* **Trade-off:** Two rows instead of one. They differ only at held-out top 5.

#### Decision 8: Held-out is scored in process, and only its headline numbers are kept

* **Decision:** Scoring calls RCAEval's evaluator (`load_answers`, `load_predictions`,
  `evaluate`) in Python, not through its command line. For held-out, the per-case rows
  and the per-fault breakdown are discarded before anything is written or printed. The
  result keeps `n_cases`, `n_predicted`, and the service and indicator scores
  (AC@1/3/5, Avg@5, Chance@5, Lift@5). Each side gets its own predictions file.
* **Context/Constraint:** The command line prints Avg@5 per fault, which would show
  held-out failures by fault type. The evaluator rejects any prediction whose case is
  not in the answers file it is given.
* **Trade-off:** The scoring path is RCAEval's own code, but not its command line.
  Chance@5 and Lift@5 are taken from the summary, which is the equivalent of
  `--report-chance`.

## 4. Constraints

* **Blind task view.** *Source: RCAEval `docs/AGENTS.md`.* **Implication:** the agent is
  given only `agent_tasks/re2-ob/`; the answers files sit beside it, never inside it.
* **The evaluator scores every case in the answers file it is given.** *Source:
  RCAEval `agent/predictions.py`.* **Implication:** a subset must be scored against a
  subset answers file, or the missing cases count as misses. Pass `--answers`
  with the subset file and omit `--tasks` when the task manifest covers all 90
  cases. RCAEval requires an exact case-ID list match if `--tasks` is supplied.
* **Predictions must match the answers file.** *Source: RCAEval
  `agent/predictions.py`, `load_predictions`.* **Implication:** a predictions file that
  contains a case not in the answers file is rejected, not ignored. Write one
  predictions file per side.
* **Closed answer vocabulary.** *Source: `task.json` candidate lists.* **Implication:**
  predictions must use exact names from those lists; there is no fuzzy matching.
* **Fault start time is given.** *Source: `task.json` `inject_time`.* **Implication:**
  detection is not being measured, and the report says so.
* **BARO uses metrics only.** *Source: RCAEval.* **Implication:** the agent also sees
  logs and traces, so the comparison is not like for like; the report states the
  difference in inputs.
* **The baselines cost nothing.** **Implication:** this whole phase spends $0.

## 5. Verification evidence
**Split (Workstream A)**

- `make split` created `splits/re2-ob.json` from the RE2-OB answers file (SHA-256
  `9ccb21e7…fcc814`): 30 groups of three; tuning 10 groups / 30 cases, held-out
  20 groups / 60 cases. Seed 2026, accepted on draw 2.
- Tuning covers all 6 fault types and all 5 services; checked by a script that
  printed counts only.
- A second `make split` verified the file and left it byte-for-byte unchanged.
- `agent_tasks/tuning.answers.jsonl` and `agent_tasks/heldout.answers.jsonl` were
  written beside the full answers file; both are gitignored.
- The committed split file contains no service or fault name (checked by `grep`).
- `make lint` and `make test` passed: 29 tests, including the check that the committed
  split verifies against the local export. That test skips in CI, which has no
  dataset.
- No label or baseline result was read before the split was committed. Spend: $0.

**Baselines (Workstream B)** — `make baselines`; all 90 cases ranked, none failed.
Service level, then indicator level (AC@1 / AC@3 / AC@5 / Avg@5). Chance@5 is 0.25 at
service level and 0.05 at indicator level on both sides.

| Baseline | Side | n | Service | Indicator |
|---|---|---|---|---|
| BARO | tuning | 30 | 0.00 / 1.00 / 1.00 / 0.79 | 0.00 / 0.53 / 0.70 / 0.46 |
| BARO | held-out | 60 | 0.22 / 0.82 / 0.92 / 0.72 | 0.13 / 0.38 / 0.52 / 0.36 |
| mmbaro | tuning | 30 | 0.03 / 1.00 / 1.00 / 0.79 | 0.03 / 0.50 / 0.70 / 0.45 |
| mmbaro | held-out | 60 | 0.23 / 0.82 / 0.95 / 0.74 | 0.15 / 0.40 / 0.50 / 0.36 |
| mmbaro, candidates only | tuning | 30 | 0.03 / 1.00 / 1.00 / 0.79 | 0.03 / 0.50 / 0.70 / 0.45 |
| mmbaro, candidates only | held-out | 60 | 0.23 / 0.82 / 1.00 / 0.75 | 0.15 / 0.40 / 0.52 / 0.37 |

Result files: `results/phase-02/*_tuning.json` and `*_heldout.json`. The held-out files
hold aggregate keys only. Per-case rows and predictions stay in gitignored
`runs/phase-02/`.

**Tuning breakdown** (`results/phase-02/tuning_breakdown.md`)

- BARO misses all 30 tuning cases at top 1 and none at top 3. The true service is
  ranked 2nd in 28 cases and 3rd in 2. This holds for every fault type and every
  service.
- The cause is one series: `redis_diskio` is BARO's first-ranked name in all 30
  tuning cases and mmbaro's in 29 (Decision 6). Redis is never the root cause in
  tuning.
- The headroom is therefore at top 1, and at indicator level (AC@3 0.53). It is not at
  top 3 at service level.
- The held-out top 1 is higher (0.22). Why was not examined: held-out is seen as
  aggregates only.

**Manual check** — the first ten tuning top-1 misses, using RCAEval's toolkit:
`rank_anomalies` on logs, trace latency and trace errors, `trace_summary` and
`search_logs`.

- **Traces pointed to the true root-cause service in 10 of 10.** Either the top
  `trace_summary` rows were that service's spans, or calls into it, or the trace-latency
  ranking put it first.
- **Logs pointed to it in 1 of 10.** Log-template rankings were led by `frontend`,
  `paymentservice` or `adservice` noise.
- This supports giving the agent trace tools early. Logs alone are weak evidence on
  RE2-OB.

**Cost per case** (`results/phase-02/cost_estimate.json`, `make headroom`; $0 spent)

- Measured on the 30 tuning cases:
  - RCAEval's reference prompt and tool schemas: 4,619 characters, about 1,155 tokens.
  - Eight probe tool calls per case, 240 results in all. After the 6,000-character
    cap: mean 2,124 characters (about 531 tokens), median 992, p90 5,145 (about 1,287
    tokens).
- Assumed: 15 tool calls plus a final turn, 150 output tokens per turn, the whole
  history re-sent every turn, four characters per token. Prices are LiteLLM 1.109.1's
  bundled map, read offline.

| Model | Typical, no cache | Typical, cached | Heavy (p90), no cache | Heavy, cached |
|---|---|---|---|---|
| claude-haiku-5-5 | $0.011 | $0.003 | $0.020 | $0.005 |
| gpt-5.4-mini | $0.086 | $0.026 | $0.154 | $0.040 |
| gemini/gemini-flash-latest | $0.084 | $0.024 | $0.152 | $0.039 |

- Against the $0.05 target: Haiku 5.5 fits even uncached. The other two fit only with
  prompt caching or context trimming (Phases 04 and 05). Uncached, they are 1.7–3×
  over.
- This is an estimate from the reference toolkit's outputs. The pilot (Phase 06)
  replaces it with measured cost.

**Target rule (Decision 3):** BARO's tuning top-1 service accuracy is 0.00, below 0.90.
**The target stays RE2-OB as exported, with the fault start time given.**

**Checks:** `make lint` and `make test` passed (48 tests). New tests cover the
breakdowns, the cost model and its configuration, BARO and mmbaro on synthetic cases,
and aggregate-only scoring.

**Resolved — stronger statistical row:** an agent could beat BARO at top 1 just by
ignoring `redis_diskio`. The owner decided to add a guarded BARO row in Phase 06. It
skips any metric with zero interquartile range in the normal window. The headline
comparison is against whichever of BARO and guarded BARO is stronger at tuning top 1
(Phase 06, Decision 7). It is not built here, so this phase's baseline is the one
Decision 3 names.

## 6. AI-use notes (feeds `REPORT.md`)
- Claude Code (Opus 5.5) drafted the Phase 02 plan, which the owner approved. The
  owner chose two PRs (split first) and LiteLLM's offline price map for the cost
  estimate.
- Claude Code wrote the answers and split modules, `scripts/make_split.py` and their
  tests. It added Decisions 4 and 5 and generated the split, reading counts only.
- Claude Code wrote the RCAEval bridge, the headroom module, `run_baselines.py`,
  `headroom_report.py` and their tests. It ran both scripts and diagnosed the
  `redis_diskio` artifact. It tried and rejected RCAEval's `dk_select_useful`
  variant, and did the manual check of ten tuning misses. It read tuning labels only;
  held-out was seen as aggregate scores only.
- The owner records the outcome of the PR logic and quality reviews here.
