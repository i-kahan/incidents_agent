## TL;DR

<!-- One or two sentences: what this PR does and why. -->

## What changed

<!-- Notable changes grouped by area. Not a file list. -->

## Why

<!-- Link the phase file and the checklist items this closes. -->

## How it was tested

<!-- Commands run and their outcome, new tests, anything checked by hand. -->

## Evaluation impact

<!-- Agent, tool or prompt changes: before/after tuning-set scores, run cost, result
file. Otherwise: None. -->

## Spend

<!-- LLM cost of this PR, and the running total from the spend guard. -->

## Risks and follow-ups

<!-- What could break, what was left out on purpose, where it is tracked. -->

## Checklist

- [ ] `make lint` passes (ruff and mypy)
- [ ] No new `# type: ignore` without an error code and a reason
- [ ] `make test` passes, no new skipped tests
- [ ] New or changed behaviour has tests in this PR
- [ ] I read the full diff as a reviewer would
- [ ] No secrets, benchmark answers or uploaded data in the diff
- [ ] No debug prints, commented-out code or unexplained TODOs
- [ ] Public functions have type annotations and Google-style docstrings
- [ ] The owning phase file is updated (status, checklist, decisions, AI-use notes)
- [ ] Architecture SVGs updated if a component changed
- [ ] No model id or price hard-coded in Python
