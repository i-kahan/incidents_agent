# Phase 2: Baseline & Headroom Study

**Status:** In progress — split locked (Workstream A); baselines and headroom next

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
- [ ] Run RCAEval's BARO example agent on all 90 cases; save `predictions.jsonl`.
- [ ] Score tuning and held-out separately, each against its own answers subset, with
      `--report-chance`. For held-out cases keep **aggregate scores only**: no per-case
      output is written or read.
- [ ] If it runs in the pinned setup, also run multi-source BARO (`mmbaro`), which uses
      metrics, logs and traces. It separates "the agent reasons better" from "the agent
      sees more data".

**Headroom analysis — tuning cases only**
- [ ] Break BARO's tuning scores down by fault type and by service; list its misses at
      top 1 and at top 3.
- [ ] For up to ten tuning misses, check by hand whether logs or traces point to the
      true root cause. Record the count.
- [ ] Estimate cost per case: tokens in a typical case's tool outputs, times the tool
      call limit, at the configured models' prices. Compare with the cost per case the
      final-run allocation implies — $0.05 at the default limit (Phase 06, Decision 6).
- [ ] Apply the target rule (Decision 3) and record the outcome.
- [ ] Unit tests for the breakdown code.

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

## 4. Constraints

* **Blind task view.** *Source: RCAEval `docs/AGENTS.md`.* **Implication:** the agent is
  given only `agent_tasks/re2-ob/`; the answers files sit beside it, never inside it.
* **The evaluator scores every case in the answers file it is given.** *Source:
  RCAEval `agent/predictions.py`.* **Implication:** a subset must be scored against a
  subset answers file, or the missing cases count as misses. Pass `--answers`
  with the subset file and omit `--tasks` when the task manifest covers all 90
  cases. RCAEval requires an exact case-ID list match if `--tasks` is supplied.
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

*Baseline tables, the tuning-set breakdown and the manual check go here.*

## 6. AI-use notes (feeds `REPORT.md`)
- Claude Code (Opus 5.5) drafted the Phase 02 plan, which the owner approved. The
  owner chose two PRs (split first) and LiteLLM's offline price map for the cost
  estimate.
- Claude Code wrote the answers and split modules, `scripts/make_split.py` and their
  tests. It added Decisions 4 and 5 and generated the split, reading counts only.
- The owner records the outcome of the PR logic and quality reviews here.
