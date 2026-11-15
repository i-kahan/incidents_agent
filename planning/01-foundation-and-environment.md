# Phase 1: Foundation & Environment

**Status:** Not started

## 1. Objective
Get a clean, reproducible project skeleton: dependencies installed, lint and tests
runnable, configuration files in place, and the RCAEval dataset exported as a blind task
view. Ends with an empty but working package that every later phase builds on.

## 2. Next Steps (Checklist)
- [ ] Confirm RCAEval's supported Python versions and pin one in `.python-version`.
- [ ] `uv init` the package (`src/incidents_agent/`), with `pyproject.toml` declaring
      runtime dependencies and a `dev` group (pytest, pytest-cov, ruff, mypy).
- [ ] Configure `ruff` in `pyproject.toml`: line length 80, Google docstring convention,
      import rules matching the style guide. Configure `ruff format`.
- [ ] Configure `mypy` in `pyproject.toml`: strict mode over `src/`, `scripts/` and
      `app/`; tests checked without the untyped-definition rule. Record any library
      that needs a typed wrapper or stub.
- [ ] Configure pytest in `pyproject.toml`: `tests/` path, the `integration` and `live`
      markers, `live` deselected by default (see `docs/practices/testing.md`).
- [ ] Add a `Makefile` with `test`, `lint` (ruff and mypy), `format`, `eval`, `compare`, `app` targets.
- [ ] Extend the existing `.gitignore` if the scaffold adds new generated paths, and add
      `.env.example` listing `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`.
- [ ] Add `config/models.yaml` with placeholder entries for the three providers.
- [ ] Add `config/budget.yaml` with the default limit ($15 total, $1 safety margin).
- [ ] CI: `.github/workflows/ci.yml` runs `make lint` and `make test` on every pull
      request and on pushes to `main`, on the pinned Python version, with the `uv`
      cache. It has no secrets, never runs `live` tests, and dataset-dependent tests
      skip. The owner enables "require status checks to pass" on `main` in GitHub.
- [ ] `LICENSE` for the repo (see the open item below).
- [ ] `NOTICE.md`: RCAEval's code and dataset licences, as found in its repository and
      dataset page, and the papers to cite. Nothing from the dataset is committed.
- [ ] `README.md` stub: one paragraph on what the project is, and a pointer to
      `DESIGN.md` and `planning/`. The full README is written in Phase 10.
- [ ] Install RCAEval; record the installed version and pin it. Try the lighter `core`
      extra first, since only its export, BARO and scoring are needed. If its
      dependencies conflict with LangGraph or LiteLLM, isolate it in its own `uv`
      dependency group or environment and call it through its CLI.
- [ ] Export RE2-OB with `rcaeval-agent export`; record the download size and case count.
- [ ] Open three cases by hand and record the actual columns of `metrics.csv`,
      `logs.csv` and `traces.csv` in Phase 03's file.
- [ ] One smoke test (`tests/test_package.py`) so `make test` and `make lint` are green.
- [ ] Cold smoke gate, before any agent code: export one case, run RCAEval's BARO
      example on it, write a `predictions.jsonl`, and score it against a matching
      one-case answers file. `make smoke` runs it at no cost.
- [ ] Check the four architecture SVGs against the scaffolded layout; fix any mismatch.
- [ ] Create the working branch `feature/phase-01-foundation`; keep `main` clean.

## 3. Architectural Decisions & Trade-offs

#### Decision 1: `uv`, `ruff`, `mypy` and `pytest` as the toolchain

* **Decision:** `uv` manages the environment and lockfile, `ruff` does linting and
  formatting, `mypy` in strict mode checks types, and `pytest` runs all tests. `make
  lint` runs ruff and mypy together.
* **Context/Constraint:** The style guide requires type annotations on public APIs;
  without a checker they are documentation that can silently go wrong. Tool results,
  ledger entries and model responses are passed between many components as structured
  data, which is where type errors hide.
* **Trade-off:** Strict mode costs effort at the boundaries with untyped libraries.
  Those get narrow, typed wrappers; `# type: ignore` is allowed only with an error code
  and a reason, and `Any` stays out of public signatures.

#### Decision 2: One branch and one PR per phase

* **Decision:** Each phase (or workstream inside a large phase) is built on its own
  `feature/phase-NN-<name>` branch and merged by squash through a PR that follows
  `docs/practices/pull-requests.md`.
* **Context/Constraint:** The git history is the record of how the work was done.
* **Trade-off:** Ceremony for a solo project, in exchange for a readable history and a
  written record of what each change did and how it was tested.

#### Decision 3: Benchmark data stays out of git

* **Decision:** The RCAEval download, the exported task view and the answers file are
  gitignored; a `make` target re-creates them.
* **Context/Constraint:** RCAEval's case ids are deterministic, so an export on any
  machine matches. The dataset is large and not ours to redistribute.
* **Trade-off:** A fresh clone needs a download step before the first eval.

#### Decision 4: CI enforces the gate the PR checklist describes

* **Decision:** Lint, type checks and the default test suite run automatically on every
  PR, and `main` requires them to pass.
* **Context/Constraint:** The PR practices say "green before merge". With several
  streams merging in parallel, a rule that depends on each author remembering to run
  two commands will eventually be skipped.
* **Trade-off:** CI cannot run anything that needs a key or the dataset, so paid paths
  and benchmark-dependent tests are still checked only locally.

## 4. Constraints
*Fixed from outside this phase. Listed with their source and implication.*

* **Google Python Style Guide.** *Source: owner.* **Implication:** ruff is configured to
  80-character lines and Google docstrings from the first commit, so no later reformat.
* **pytest.** *Source: owner.* **Implication:** no `unittest.TestCase` classes anywhere.
* **Three providers, owner-supplied keys.** *Source: owner.* **Implication:** the
  skeleton ships `.env.example` and `config/models.yaml` with placeholders only.

**⚠️ OPEN — Python version:** RCAEval's README mentions a Python 3.14 default
environment; whether its agent extra installs cleanly alongside LangGraph and LiteLLM on
one version is unverified. First checklist item.

**⚠️ OPEN — Licence:** the repo is public and has no licence, which means nobody else
may legally reuse it. MIT is proposed as the default; the owner decides.

**Resolved — Git remote:** the repo has a GitHub remote (`origin`), so PRs are opened
there using `.github/pull_request_template.md`.

## 5. AI-use notes (feeds `REPORT.md`)
*Filled in as the work happens: where AI was used, what was accepted, modified, rejected.*
