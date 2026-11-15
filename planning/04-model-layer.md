# Phase 4: Model Layer

**Status:** Not started

## 1. Objective
One place through which every model call goes: provider-agnostic, configured by the
owner, with exact cost accounting and a hard spending limit. Ends with a client the
agent can call with any configured model, and a guard that makes overspending impossible.

## 2. Next Steps (Checklist)
- [ ] `config/models.yaml` loader with validation: alias → LiteLLM model string, optional
      price override, a `default` alias.
- [ ] Key discovery from `.env`: report which providers are usable; a model whose key is
      missing is skipped with a notice, not an error.
- [ ] Chat client wrapping LiteLLM: temperature 0, tool calling, bounded retries with
      backoff on transient errors, a per-call output-token cap.
- [ ] Cost accounting per call from token usage and price (LiteLLM's table or the
      override). A model with no known price is refused.
- [ ] Spend guard as an admission rule. Before a call is sent, an upper bound on its
      cost (input tokens counted, plus the output-token cap, at the model's price) is
      reserved atomically against the global limit and, when given, the per-case cap. A
      call that cannot be reserved is refused before any money is spent. After the call
      the reservation is settled to the actual cost.
- [ ] A reservation whose call timed out or failed ambiguously is kept at its upper
      bound until reconciled, never released on the assumption that nothing was charged.
- [ ] Every paid path reserves through the guard: agent calls, retries (each attempt is
      its own reservation), the ingest fallback, `check_models.py`, `live` tests, and
      any paid baseline.
- [ ] Named reserves: an amount can be set aside under a name (`final`) so that other
      runs cannot spend it; only a run started with that name can draw on it.
- [ ] Budget configuration in `config/budget.yaml`, validated on load: `total_usd`
      (default 15.00), `safety_margin_usd` (default 1.00), `per_case_cap_usd`, and
      `reserves` by name. The guard's limit is the total minus the margin. No dollar
      amount appears in Python.
- [ ] `--budget-usd` on the scripts and `INCIDENTS_BUDGET_USD` in the environment
      override the file's total for one invocation; the app shows the limit and the
      amount left but cannot change it.
- [ ] The limit in force is written into every run manifest and trajectory, so a result
      always shows the budget it was produced under.
- [ ] Changing the limit never resets what has been spent. Lowering it below the amount
      already spent or reserved makes the guard refuse every further call; raising it
      takes effect on the next call. Reserves that no longer fit the limit are a
      configuration error reported at start-up.
- [ ] `scripts/budget.py`: prints the limit, spent, reserved and remaining amounts, and
      the allocation table of Phase 06 scaled to the current limit.
- [ ] The guard's ledger is a file under `runs/`, updated under a lock, safe across
      processes.
- [ ] Per-run budget object: token, cost and call counters the agent can check.
- [ ] `scripts/check_models.py`: lists configured models, whether each is usable, its
      price, and makes one tiny live call per usable model on request.
- [ ] A scripted fake client for tests: returns queued responses and tool calls.
- [ ] Unit tests: config validation, missing keys, price override, reservation and
      settlement arithmetic, refusal at the limit, a limit read from configuration and overridden per invocation,
      a limit lowered below the amount already spent, reserves larger than the limit
      rejected at load, a named reserve that other runs
      cannot touch, an ambiguous timeout that stays reserved, retry behaviour with a
      failing fake.
- [ ] Concurrency test: two workers compete for a remaining balance that covers one
      call; exactly one is admitted and the total never exceeds the limit.
- [ ] One `live` test per provider (opt-in, a few tokens each).

## 3. Architectural Decisions & Trade-offs

#### Decision 1: LiteLLM as the provider abstraction

* **Decision:** All calls go through LiteLLM; the rest of the code never imports a
  provider SDK.
* **Context/Constraint:** Three providers must be swappable and comparable, and
  LiteLLM already normalises tool calling and reports usage.
* **Trade-off:** A dependency sits between the code and the providers; provider-specific
  features such as prompt caching are used only where LiteLLM exposes them.

#### Decision 2: Models are configuration, not code

* **Decision:** No model id appears in Python. The owner lists models in
  `config/models.yaml` and keys in `.env`.
* **Context/Constraint:** The owner decides which keys and models to provide, and model
  ids change faster than the code should.
* **Trade-off:** A fresh clone does nothing useful until the file is filled in;
  `check_models.py` exists to make that step quick to verify.

#### Decision 3: The spend guard admits calls; it does not just record them

* **Decision:** Each call reserves its worst-case cost before it is sent and settles to
  the actual cost afterwards. The limit is configured, not coded: by default the guard stops $1 short
  of a $15 total. It refuses any model
  whose price it cannot determine, and holds a named reserve for the final run.
* **Context/Constraint:** A guard that checks the total and writes the charge after
  each call has two holes: one large call can pass the limit by itself, and several
  parallel workers can each see room and all spend it. Evals run in parallel and
  unattended, so both will happen eventually.
* **Trade-off:** Worst-case reservations are larger than actual costs, so near the
  limit the guard refuses calls that would have fitted, and parallelism is throttled.
  The safety margin is left unused, and a new model needs a price entry before its first
  call. Because the limit is configurable, raising it is one edit; that is deliberate,
  and every result records the limit it ran under so a quiet increase is visible.

#### Decision 4: Temperature 0, no repeated sampling

* **Decision:** Every call uses temperature 0 and each case is run once per
  configuration.
* **Context/Constraint:** The budget allows a handful of full passes, not repeats.
* **Trade-off:** Run-to-run variance is not measured. The report states this and relies
  on bootstrap intervals over cases instead.

## 4. Constraints

* **A dollar limit, configurable, $15 by default; mostly cheap models.** *Source:
  owner.* **Implication:** the guard is built before the agent, so the first agent run
  is already protected, and nothing downstream assumes a particular amount.
* **OpenAI, Anthropic and Google.** *Source: owner.* **Implication:** tests cover the
  case where only one of the three keys is present.

**⚠️ OPEN — Models to configure:** the owner supplies keys and model ids. The exact
cheap models are recorded here once `check_models.py` has confirmed them live.

## 5. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens.*
