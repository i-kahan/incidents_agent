# Incidents Agent

- Design: [DESIGN.md](DESIGN.md)
- Plan: [planning/00-high-level-architecture-planning.md](planning/00-high-level-architecture-planning.md)
  is the hub for the core pipeline (Phases 01–10); each phase has its own file in
  `planning/`. Additional features (Phases 11–17) are in
  [planning/additional-features/](planning/additional-features/README.md) and start
  only after the core pipeline works end to end.

## Working rules

1. **Follow the plan.** Work on one phase at a time, from its checklist. Update the
   phase file in the same change: status, checked boxes, decisions, AI-use notes.
2. **Tests ship with the code**, using pytest. Rules:
   [docs/practices/testing.md](docs/practices/testing.md).
3. **One PR per phase or workstream**, on a `feature/phase-NN-<name>` branch. Rules and
   description template:
   [docs/practices/pull-requests.md](docs/practices/pull-requests.md).
4. **Keep the architecture SVGs in `planning/` in sync** when a component is added,
   removed or rewired.
5. **Never spend LLM budget without saying so first.** The limit is set in `config/budget.yaml` ($15 by default); do not
   raise it or override it without being asked. Paid
   runs go through the spend guard and the run cache; held-out cases run only in
   Phase 10.
6. **`REPORT.md` is written in Phase 10** by aggregating the phase files. Do not record
   a result there, or anywhere, before it has been produced.

## Code style

All Python in this repo follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
A local copy may exist at `docs/style/google_pyguide.md` (gitignored; download it from
https://raw.githubusercontent.com/google/styleguide/gh-pages/pyguide.md if missing).
Consult it when unsure.

Rules that come up most:

- 80-character lines, 4-space indentation, no backslash line continuation.
- Imports: one per line, at module top, absolute (full package path), no relative
  imports. Import modules and packages, not individual names (exceptions: `typing`,
  `collections.abc`).
- Type annotations on all public functions and methods.
- Google-style docstrings (`Args:`, `Returns:`, `Raises:`) on every module, public class
  and public function, unless the function is short and obvious.
- Naming: `module_name`, `ClassName`, `function_name`, `CONSTANT_NAME`, `_private`.
- No mutable default arguments. No mutable global state.
- Catch specific exceptions; never bare `except:`.
- Logging calls pass a `%`-pattern string and arguments, not f-strings.
- Scripts put their logic in `main()` behind `if __name__ == '__main__':`.
- TODOs use `# TODO: <link or context> - <explanation>`.

Enforcement: `ruff` (line length 80, Google docstring convention, import rules) and
`ruff format` are configured in `pyproject.toml`, with `mypy` in strict mode for types.
`make lint` runs all three; run it before finishing any change. Use `# type: ignore`
only with an error code and a reason, and keep `Any` out of public signatures.
