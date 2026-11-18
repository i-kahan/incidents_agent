# Phase 4: Model Layer

**Status:** Complete. Implemented, checked offline, and verified live on `gpt-small` (`gpt-4o-mini-2024-07-18`), `gemini-small`, and `haiku` across tuning and Phase 10 held-out runs.

## 1. Objective
One place through which every model call goes: provider-agnostic, configured by the
owner, with exact cost accounting and a hard spending limit. Ends with a client the
agent can call with any configured model, and a guard that makes overspending impossible.

## 2. Next Steps (Checklist)
- [x] `config/models.yaml` loader with validation: alias → LiteLLM model string, optional
      price override, a `default` alias.
- [x] Key discovery from `.env`: report which providers are usable; a model whose key is
      missing is skipped with a notice, not an error.
- [x] Chat client wrapping LiteLLM: temperature 0, tool calling, bounded retries with
      backoff on transient errors, a per-call output-token cap.
- [x] Rate limits: a concurrency cap per provider, `Retry-After` honoured, backoff with
      jitter. A rate-limited attempt is not a case failure, but every attempt still
      reserves through the spend guard.
- [x] Served-model record: each call stores the model identifier the provider returned,
      the request id and the time. `config/models.yaml` uses pinned or dated model ids
      wherever a provider offers them, and the loader warns on a floating alias.
      Owner IDs are still pending.
- [x] Prompt caching where the provider supports it through LiteLLM: the system prompt,
      tool schemas and catalog briefing form a stable prefix, in that order. Cost
      accounting uses the cached-token price. Support is confirmed per provider and
      recorded here; where it is absent, nothing else changes.
- [x] Cost accounting per call from token usage and price (LiteLLM's table or the
      override). A model with no known price is refused.
- [x] Spend guard as an admission rule. Before a call is sent, an upper bound on its
      cost (input tokens counted, plus the output-token cap, at the model's price) is
      reserved atomically against the global limit and, when given, the per-case cap. A
      call that cannot be reserved is refused before any money is spent. After the call
      the reservation is settled to the actual cost.
- [x] A reservation whose call timed out or failed ambiguously is kept at its upper
      bound until reconciled, never released on the assumption that nothing was charged.
- [x] Every paid path reserves through the guard: agent calls, retries (each attempt is
      its own reservation), the ingest fallback, `check_models.py`, `live` tests, and
      any paid baseline. The classifier, agent, and baselines adopt the contract
      in their owning phases.
- [x] Named reserves: an amount can be set aside under a name (`final`) so that other
      runs cannot spend it; only a run started with that name can draw on it.
- [x] Budget configuration in `config/budget.yaml`, validated on load: `total_usd`
      (default 15.00), `safety_margin_usd` (default 1.00), `per_case_cap_usd`, and
      `reserves` by name. The guard's limit is the total minus the margin. No dollar
      amount appears in Python.
- [x] `--budget-usd` on the scripts and `INCIDENTS_BUDGET_USD` in the environment
      override the file's total for one invocation. The app reads the ledger
      snapshot in Phase 08.
- [x] The limit in force is written into every run manifest and trajectory, so a result
      always shows the budget it was produced under. The probe writes both now;
      Phases 05 and 06 consume the same metadata.
- [x] Changing the limit never resets what has been spent. Lowering it below the amount
      already spent or reserved makes the guard refuse every further call; raising it
      takes effect on the next call. Reserves that no longer fit the limit are a
      configuration error reported at start-up.
- [x] `scripts/budget.py`: prints the limit, spent, reserved and remaining amounts, and
      the allocation table of Phase 06 scaled to the current limit. Named
      reserves keep their configured dollar values.
- [x] The guard's ledger is a file under `runs/`, updated under a lock, safe across
      processes.
- [x] Per-run budget object: token, cost and call counters the agent can check.
- [x] `scripts/check_models.py`: lists configured models, whether each is usable, its
      price, whether its id is pinned, and makes one tiny live call per usable model on
      request, reporting the model identifier actually served.
- [x] A scripted fake client for tests: returns queued responses and tool calls.
- [x] Unit tests: config validation, missing keys, price override, reservation and
      settlement arithmetic, refusal at the limit, a limit read from configuration and overridden per invocation,
      a limit lowered below the amount already spent, reserves larger than the limit
      rejected at load, a named reserve that other runs
      cannot touch, an ambiguous timeout that stays reserved, retry behaviour with a
      failing fake.
- [x] Concurrency test: two workers compete for a remaining balance that covers one
      call; exactly one is admitted and the total never exceeds the limit.
- [x] Tests with the fake: `Retry-After` is honoured, the per-provider cap holds under
      parallel calls, a served model id different from the configured one is recorded
      and warned about, and cached-token pricing is applied.
- [x] One `live` test per provider (opt-in, a few tokens each).

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
  The worst case is bounded by the request's UTF-8 size, not the model's context
  window, so it overstates input about 3-4 times rather than by up to 1M tokens. A
  provider that bills hidden input beyond that allowance shows up as a reported
  overrun, which blocks further admissions until reconciled.
  The safety margin is left unused, and a new model needs a price entry before its first
  call. Because the limit is configurable, raising it is one edit; that is deliberate,
  and every result records the limit it ran under so a quiet increase is visible.

#### Decision 4: Temperature 0, one run per configuration, noise measured once

* **Decision:** Every call uses temperature 0 and each case is run once per
  configuration. Run-to-run variation is measured once, on a small sample, by the noise
  check in Phase 06, and that figure is quoted beside every comparison.
* **Context/Constraint:** The budget allows a handful of full passes, not repeats.
  Temperature 0 does not make a provider deterministic, so without any measurement a
  difference between two prompts cannot be told from noise.
* **Trade-off:** The noise estimate comes from few cases and one model, and is assumed
  to carry over to other configurations.

#### Decision 5: Record what was served, not only what was asked for

* **Decision:** Every call records the provider's reported model identifier, and model
  ids are pinned where the provider allows.
* **Context/Constraint:** A floating alias can start pointing at a different model
  between the tuning runs and the final run, changing results with no change in the
  repo.
* **Trade-off:** Pinned ids need updating by hand, and not every provider offers them.

## 4. Constraints

* **A dollar limit, configurable, $15 by default; mostly cheap models.** *Source:
  owner.* **Implication:** the guard is built before the agent, so the first agent run
  is already protected, and nothing downstream assumes a particular amount.
* **OpenAI, Anthropic and Google.** *Source: owner.* **Implication:** tests cover the
  case where only one of the three keys is present.

**⚠️ OPEN — Models to configure:** the owner supplies keys and model ids. The exact
cheap models are recorded here once `check_models.py` has confirmed them live.

## 5. Client contract and operation

- Consumers use `contracts.ModelClient.complete(request, run=run, case_id=...)`.
  `ChatRequest` separates the system prompt, ordered function schemas, catalog
  briefing, and history. Append the returned assistant message as-is before
  returning a tool result; some providers include fields needed for the next turn.
  Use a unique `Run.id` for each independent run and share it with its workers.
  Set `Run.reserve_name` when starting a run that draws from a named reserve.
- `GuardedClient` implements the paid path, and `fake.ScriptedClient` provides
  queued messages or errors at no cost. Handle `RetryDeferred.retry_after` by
  rescheduling, `BudgetRefused` by stopping that spending path, and
  `BillingUncertain` by retaining the attempt for reconciliation. Use
  `SpendLedger.counters(run)`, `attempt_records(run)`, and `snapshot()` for
  run records and budget display.
- Each attempt reserves for an input bound of the request's UTF-8 size (system
  prompt, catalog briefing, history including provider fields, and tool schemas)
  plus 2,048 tokens and 64 per message or tool for provider framing, capped at the
  model's published input ceiling. Every token the supported providers count covers
  at least one byte of text, so this cannot undercount, unlike the SDK's token
  estimate; the transport may still supply a tighter proven bound. A 10-tool first
  call with a 6,000-character briefing bounds at about 17,000 tokens rather than
  the full window. An owner override for a model missing from the catalog needs paired
  `input_cost_per_mtok` and `output_cost_per_mtok` rates and the published hard
  `max_input_tokens`. Optional cache rates are `cached_input_cost_per_mtok` and
  `cache_write_cost_per_mtok`. The accounting relies on accurate published caps
  and rates. A reported overrun is recorded and blocks further admissions pending
  reconciliation.
- Start from the project root. `uv run --locked python -m scripts.check_models`
  inventories models offline. With owner configuration and authorization after
  merge, add `--live` to send one guarded attempt per usable model. The probe
  writes a manifest and trajectory under `runs/`.
- `uv run --locked python -m scripts.budget` reports ledger balances and the
  scaled evaluation allocation. Both commands accept `--budget-usd` for the
  invocation. With confirmed billing evidence and no worker still using the
  reservation, run
  `uv run --locked python -m scripts.budget --reconcile ATTEMPT_ID --actual-usd AMOUNT --evidence REFERENCE`.
  The SQLite ledger persists spend across restarts. Use the same project ledger
  for all paid workers.

### Provider cache support

| Provider | Behavior in the locked LiteLLM adapter | Evidence |
|---|---|---|
| OpenAI | Automatic caching for eligible models | Offline HTTP fixture preserves the stable prefix and reports cache reads |
| Anthropic | An ephemeral checkpoint at the end of the stable briefing, or at the end of the system prompt when there is no briefing. Anthropic orders tools ahead of system text on the wire. | Offline HTTP fixture checks the checkpoint and normalizes reads and writes |
| Gemini | Implicit caching for eligible models | Offline HTTP fixture preserves the prefix, normalizes cached and reasoning tokens, and verifies that an SDK continuation cannot dispatch under the same reservation |

These fixtures exercise the locked SDK. Live cache hits are unverified. Provider
documentation: [LiteLLM prompt caching](https://docs.litellm.ai/docs/completion/prompt_caching),
[Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[Gemini context caching](https://ai.google.dev/gemini-api/docs/caching).

## 6. AI-use notes (feeds `REPORT.md`)

Codex implemented the typed client, reservation ledger, CLI, and tests in this
phase. The design uses one SQLite transaction per admission and an HTTP dispatch
check so LiteLLM or an SDK cannot retry under an existing reservation. Verification
uses mock HTTP, a real ledger race between two processes, and fake transport
timeouts. `make lint` passed. The default suite passed 67 tests with 93.59%
line coverage for `llm`. The three opt-in live tests were deselected. LLM spend
for phase implementation and verification was $0.00.

Reservation fix: a Wave 2 review found every attempt reserved the model's full input
window, $0.07 to $0.79 per attempt on the candidate models against an estimated
$0.01 to $0.15 per case, and held that amount after each ambiguous timeout. Claude
added `client.request_input_bound`, which bounds input tokens by the request's UTF-8
size plus framing allowances. With the bundled prices, a first agent call now
reserves $0.003 to $0.022, and a call after 30 tool results $0.06 to $0.17. Three
tests were added; LLM spend was $0.00.
