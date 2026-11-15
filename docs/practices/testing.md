# Testing Practices

When tests are written in this repo and what they look like. The framework is
**pytest**; there are no `unittest.TestCase` classes. Test code follows the Google
Python Style Guide like all other code.

## 1. When tests are written

Tests are written with the code, in the same PR. There is no later "testing phase".

| Change | Tests required in the same PR |
|---|---|
| New public function, class or tool | At least one unit test for the normal case and one per documented error or edge case |
| New code path across a boundary (file system, DuckDB, model client, RCAEval, Streamlit run function) | One integration test through the real boundary, or its fake where the real one costs money |
| Bug fix | A regression test that fails before the fix and passes after; written first |
| Behaviour change | The existing tests updated in the same PR; a test is never deleted to make a change pass |
| Refactor with no behaviour change | No new tests; the existing ones must pass untouched |
| Prompt or tool-description change | No unit test; an evaluation run on the tuning set, reported in the PR |
| New rule the system must always obey (grounding, read-only SQL, answers hidden, spend limit) | A test that tries to break the rule and asserts it holds |

**How often they run:**

- `make test` before every commit that touches Python, and before opening a PR.
- The full default suite must pass on the final commit of every PR.
- `integration` tests run in the default suite. `live` tests run only on request.

**Coverage:** measured with `pytest-cov` and reported in each PR. The target is 85% line
coverage for `ingest`, `tools`, `agent/ledger`, `llm` and `eval`. The Streamlit layer
has no target; its logic lives in plain functions that do. Coverage is a floor for
noticing gaps, not a goal: a line that is executed but not asserted on is not tested.

## 2. Kinds of test

| Kind | Marker | Uses | Speed | In default run |
|---|---|---|---|---|
| Unit | none | One function or class; fakes for everything outside it | Milliseconds | Yes |
| Integration | `@pytest.mark.integration` | Several real components together: real DuckDB, real files in `tmp_path`, the real graph with the fake model client | Under a few seconds | Yes |
| Live | `@pytest.mark.live` | A real provider API; costs money | Seconds | No — `pytest -m live` |

Rule of thumb: if every third-party dependency is replaced by a fake, it is a unit test.

Tests that need the RCAEval dataset skip with a clear reason when it is not present, so
a fresh clone is green before any download.

**Evaluations are not tests.** An evaluation measures how good the agent is and
produces a score; a test checks that code does what it should and passes or fails.
Evaluations live in `scripts/` and `src/incidents_agent/eval/`, cost money, and never
run under pytest.

## 3. Layout and naming

```
tests/
  conftest.py              # shared fixtures
  fixtures/                # small, hand-made input files
    incident_small/
  ingest/test_loaders.py   # mirrors src/incidents_agent/ingest/loaders.py
  tools/test_run_sql.py
  agent/test_ledger.py
  ...
```

- The `tests/` tree mirrors `src/incidents_agent/`.
- Files are `test_<module>.py`. Functions are
  `test_<unit>_<condition>_<expected>`, for example
  `test_run_sql_with_insert_statement_returns_error`.
- One behaviour per test. A test that needs "and" in its name is two tests.

## 4. Structure of a test

Each test has three parts, separated by blank lines: **given** (set up), **when** (the
one action under test), **then** (assertions).

```python
def test_compare_windows_ranks_shifted_column_first(metrics_db):
    split_time = 1_700_000_600

    result = compare_windows.run(
        metrics_db, table='metrics', split_time=split_time, top_k=3
    )

    assert result['ranked'][0]['column'] == 'checkout_cpu'
    assert result['ranked'][0]['score'] > result['ranked'][1]['score']
```

- Assert on values and behaviour, never on log output alone.
- Use plain `assert`; use `pytest.raises(SpecificError, match=...)` for exceptions and
  `pytest.approx` for floats.
- No logic in tests: no loops over cases, no conditionals. Use parametrisation.
- A test must fail for one reason. If it would be unclear from the failure message what
  broke, split it.

## 5. Fixtures

- Shared fixtures live in `conftest.py` at the narrowest level that needs them.
- Use `tmp_path` for every file a test writes. Tests never write inside the repo.
- Fixture data is small and hand-made: a metrics table of a few dozen rows with one
  obvious shift is better than a slice of the real dataset.
- Prefer factory fixtures (`make_ledger(...)`) over many near-identical fixtures.
- Default to function scope. Widen the scope only for expensive, read-only setup.

```python
@pytest.fixture
def metrics_db(tmp_path):
    """A database with one metrics table that shifts at t=1_700_000_600."""
    folder = tmp_path / 'case'
    shutil.copytree(FIXTURES / 'incident_small', folder)
    return ingest.load_folder(folder)
```

## 6. Parametrised tests

Use `pytest.mark.parametrize` when the same behaviour is checked across inputs. Give
each case an id so a failure names it.

```python
@pytest.mark.parametrize(
    'query',
    [
        pytest.param('DELETE FROM metrics', id='delete'),
        pytest.param('SELECT 1; DROP TABLE metrics', id='multi_statement'),
        pytest.param("COPY metrics TO 'out.csv'", id='copy_out'),
    ],
)
def test_run_sql_with_non_select_returns_error(metrics_db, query):
    result = run_sql.run(metrics_db, query=query)

    assert 'error' in result
```

## 7. Testing code that calls a model

- **No real model calls in unit or integration tests.** They use the scripted fake
  client from the model layer, which returns queued messages and tool calls.
- A graph test scripts the model's side of a conversation and asserts on what the agent
  did: which node ran, what was written to the ledger, what was submitted, what the
  budget counters show.
- Every limit and guard has a test that drives the agent into it: the tool-call limit,
  the per-case cost cap, the global spend limit, an invalid `submit`, a tool error.
- Tests never assert on the wording of model output; with a fake there is none to
  assert on, and with a real model it is not stable.
- `live` tests exist only to confirm that each provider's wiring works. They make the
  smallest possible call and assert on shape (a response came back, usage was recorded),
  not on content.

## 8. Determinism and isolation

- No network access in the default suite.
- No dependence on test order; any test can run alone.
- Anything random takes an explicit seed.
- No real sleeps; retry and timeout logic is tested with injected clocks or zero delays.
- No reading of `.env` or real API keys; configuration is passed in or set with
  `monkeypatch`.
- The spend ledger and the incident memory are always pointed at `tmp_path` in tests.

## 9. What not to test

- Third-party libraries (DuckDB's SQL engine, LiteLLM's HTTP layer, Streamlit widgets).
- Private helpers directly, when a public function already exercises them.
- Trivial code with no logic, such as a dataclass with no methods.

## 10. Commands

```bash
make test                         # default suite: unit + integration
make test PYTEST_ARGS='-m "not integration and not live"' # unit tests only
uv run pytest -m live             # paid provider checks, on request
make test PYTEST_ARGS='--cov=incidents_agent --cov-report=term-missing'
uv run pytest tests/tools/test_run_sql.py -k non_select   # one test while working
```
