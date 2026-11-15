# Phase 1: Foundation & Environment

**Status:** Ready for review

## 1. Objective
Get a clean, reproducible project skeleton: dependencies installed, lint and tests
runnable, configuration files in place, and the RCAEval dataset exported as a blind task
view. Ends with an empty but working package that every later phase builds on.

## 2. Next Steps (Checklist)
- [x] Confirm RCAEval's supported Python versions and pin one in `.python-version`.
- [x] `uv init` the package (`src/incidents_agent/`), with `pyproject.toml` declaring
      runtime dependencies and a `dev` group (pytest, pytest-cov, ruff, mypy).
- [x] Configure `ruff` in `pyproject.toml`: line length 80, Google docstring convention,
      import rules matching the style guide. Configure `ruff format`.
- [x] Configure `mypy` in `pyproject.toml`: strict mode over `src/`, `scripts/` and
      `app/`; tests checked without the untyped-definition rule. Record any library
      that needs a typed wrapper or stub.
- [x] Configure pytest in `pyproject.toml`: `tests/` path, the `integration` and `live`
      markers, `live` deselected by default (see `docs/practices/testing.md`).
- [x] Add a `Makefile` with `test`, `lint` (ruff and mypy), `format`, `eval`, `compare`, `app` targets.
- [x] Extend the existing `.gitignore` if the scaffold adds new generated paths, and add
      `.env.example` listing `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`.
- [x] Add `config/models.yaml` with placeholder entries for the three providers.
- [x] Add `config/budget.yaml` with the default limit ($15 total, $1 safety margin).
- [x] CI: `.github/workflows/ci.yml` runs `make lint` and `make test` on every pull
      request and on pushes to `main`, on the pinned Python version, with the `uv`
      cache. It has no secrets, never runs `live` tests, and dataset-dependent tests
      skip.
- [ ] Owner enables the required `lint-and-test` check on `main` before merging.
- [x] Add the MIT `LICENSE` for the repo.
- [x] `NOTICE.md`: RCAEval's code and dataset licences, as found in its repository and
      dataset page, and the papers to cite. Nothing from the dataset is committed.
- [x] `README.md` stub: one paragraph on what the project is, and a pointer to
      `DESIGN.md` and `planning/`. The full README is written in Phase 10.
- [x] Install RCAEval; record the installed version and pin it. Try the lighter `core`
      extra first, since only its export, BARO and scoring are needed. If its
      dependencies conflict with LangGraph or LiteLLM, isolate it in its own `uv`
      dependency group or environment and call it through its CLI.
- [x] Export RE2-OB with `rcaeval-agent export`; record the download size and case count.
- [x] Open three cases by hand and record the actual columns of `metrics.csv`,
      `logs.csv` and `traces.csv` in Phase 03's file.
- [x] One smoke test (`tests/test_package.py`) so `make test` and `make lint` are green.
- [x] Cold smoke gate, before any agent code: export one case, run RCAEval's BARO
      example on it, write a `predictions.jsonl`, and score it against a matching
      one-case answers file. `make smoke` runs it at no cost.
- [x] Check the four architecture SVGs against the scaffolded layout; fix any mismatch.
- [x] Create the working branch `feature/phase-01-foundation`; keep `main` clean.

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

**Resolved — Python and RCAEval:** Python 3.12 is pinned. RCAEval 1.12.0 with
its `core` extra installs with the runtime and dev dependencies in the same uv
environment. The `benchmark` group is opt-in. Phase 01 calls RCAEval via its CLI
and adds no stubs or wrappers for untyped library APIs.

**Resolved — Licence:** The owner selected MIT for the project.
`NOTICE.md` records the third-party sources and their distribution licenses.

**Resolved — Git remote:** the repo has a GitHub remote (`origin`), so PRs are opened
there using `.github/pull_request_template.md`.

## 5. Verification evidence

- `uv sync --locked --group benchmark` installed the project and the benchmark
  tooling together on macOS arm64 with Python 3.12.12.
- `make lint` passed Ruff lint, formatting, and strict mypy. `make test`
  passed three tests, including two RCAEval CLI tests using synthetic data.
  Coverage reports 100% for the import-only package (zero executable statements).
- `make smoke` exported and scored one synthetic case, with one matching BARO
  prediction and top-1 service accuracy of 1.00. The smoke check spent $0.
- The four architecture diagrams and build-waves diagram were checked. The
  evaluator diagram now identifies trajectories as agent output.
- Git ignore rules cover raw data, the export and answers, the temporary ZIP,
  and run outputs.
- A clean local clone passed the locked install, lint, the three tests, coverage,
  and the synthetic smoke check. GitHub Actions also passed `lint-and-test`.
- `make export` downloaded the Zenodo archive (1,191,025,569 bytes) and wrote
  90 anonymised cases. The extracted raw files totalled 8,662,327,689 bytes.
  The blind manifest and case directories matched. The sibling answers file
  was verified by its path. The first three CSV headers are in
  [Phase 03](03-ingest-and-tools.md#5-dataset-notes).

## 6. AI-use notes (feeds `REPORT.md`)

- Codex drafted the implementation plan, scaffolded the phase and ran the checks
  recorded above. The owner selected MIT and the synthetic smoke case.
- The owner records the outcome of the PR logic and quality reviews here.
