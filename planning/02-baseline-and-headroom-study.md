# Phase 2: Baseline & Headroom Study

**Status:** Not started

## 1. Objective
Fix the tuning and held-out split first, then measure what the statistical baselines
already achieve on RE2-OB and where they fail, and therefore how much room an agent has
to add value. Ends with a locked split, baseline numbers on disk, and a benchmark target
confirmed or changed by a rule written down before the numbers were seen.

## 2. Next Steps (Checklist)

**Split — done first, before any labelled result is looked at**
- [ ] Group the 90 cases by service–fault pair (30 pairs, three repetitions each) using
      the answers file. The grouping is done by a script; no per-case label is printed.
- [ ] Create `splits/re2-ob.json`: 10 pairs (30 cases) for tuning, 20 pairs (60 cases)
      held out, from a fixed seed, constrained so that every fault type and every
      service appears at least once in tuning. All three repetitions of a pair are on
      the same side.
- [ ] Write the matching answers subsets (`tuning.answers.jsonl`,
      `heldout.answers.jsonl`) beside the full answers file, outside the task directory.
- [ ] Commit the split file and its seed. It does not change after this point.
- [ ] Unit tests for the split: deterministic, disjoint, sizes, no pair on both sides,
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
*Split summary, baseline tables, the tuning-set breakdown and the manual check go here.*

## 6. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
