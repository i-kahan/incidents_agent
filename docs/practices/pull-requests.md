# Pull Request Practices

How changes are proposed and merged in this repo. It applies to every phase, including
work done by AI coding tools. Code style is covered separately by the Google Python
Style Guide (https://google.github.io/styleguide/pyguide.html); tests by `docs/practices/testing.md`.

## 1. Scope and size

- **One purpose per PR.** A PR delivers one phase or one workstream of a phase. A
  refactor, a behaviour change and a formatting pass are three PRs, not one.
- **Small enough to review in one sitting.** Aim for under about 400 changed lines of
  hand-written code, tests included. Generated files, lockfiles and fixtures do not
  count but are called out in the description.
- **Split when it grows.** If a PR passes the size target, split it along the phase
  file's workstreams before asking for review.
- **Always mergeable.** `main` must pass lint and tests after every merge. Work that is
  not finished stays on its branch or behind a switch.

## 2. Branches and commits

- **Branch names:** `feature/phase-NN-<short-name>` for phase work,
  `fix/<short-name>` for bug fixes, `docs/<short-name>` for documentation only.
- **Never commit directly to `main`.**
- **Commit messages:** an imperative summary of at most 72 characters (`Add read-only
  SQL tool`), a blank line, then why the change was needed when that is not obvious.
- **Commits tell a story while the PR is open;** they are squashed on merge, so the PR
  title and description become the permanent record.
- **AI-assisted commits** carry the co-author trailer of the tool that produced them.

## 3. Title

`<Phase NN>: <what changed, in the imperative>`

Examples: `Phase 03: Add ingest loaders for CSV, Parquet and JSON lines`;
`Fix: Reject multi-statement queries in run_sql`.

## 4. Description

Every PR uses the template in `.github/pull_request_template.md`:

| Section | Content |
|---|---|
| **TL;DR** | One or two sentences: what this PR does and why. |
| **What changed** | The notable changes, grouped by area. Not a file list. |
| **Why** | The problem or plan item behind it, linking the phase file and checklist items. |
| **How it was tested** | Commands run and their outcome; new tests added; what was checked by hand. |
| **Evaluation impact** | For changes to the agent, tools or prompts: before and after scores on the tuning set, cost of the run, and the result file. `None` otherwise. |
| **Spend** | LLM cost of producing and testing this PR, and the running total from the spend guard. |
| **Risks and follow-ups** | What could break, what was left out on purpose, and where it is tracked. |

A reviewer should be able to understand the change from the description alone.

## 5. Before opening a PR

The author checks every item; the template repeats this list.

- [ ] `make lint` passes (`ruff check`, `ruff format --check` and `mypy`).
- [ ] No new `# type: ignore` without an error code and a reason; no `Any` in public
      signatures.
- [ ] `make test` passes, with no new skipped tests.
- [ ] New or changed behaviour has tests in this PR (see `docs/practices/testing.md`).
- [ ] The diff was read in full by the author, top to bottom, as a reviewer would.
- [ ] No secrets, keys, `.env` contents, benchmark answers or uploaded data in the diff.
- [ ] No debug prints, commented-out code or unexplained `TODO`s.
- [ ] Public functions have type annotations and Google-style docstrings.
- [ ] The owning phase file is updated: status, checked boxes, decisions, AI-use notes.
- [ ] The architecture SVGs are updated if a component was added, removed or rewired.
- [ ] No model id or price is hard-coded in Python.

## 6. Review

This is a solo project, so review is done in two passes before merging:

1. **Logic pass:** does the change do what the phase file says, are the edge cases
   covered by tests, and are the numbers in the description reproducible from the
   commands given?
2. **Quality pass:** style guide, naming, structure, error handling, and whether a
   simpler design would do.

When an AI tool wrote the code, the owner performs both passes and records in the phase
file's AI-use notes what was accepted, modified and rejected. A second automated review
is welcome but does not replace reading the diff.

Review comments are answered in the PR, and the fix is pushed as a new commit, not an
amended one, so the change is visible.

## 7. Merging

- **Squash merge.** One commit per PR on `main`, with the PR title as its summary and
  the TL;DR as its body.
- **Green before merge.** Lint and tests pass on the final commit.
- **Delete the branch** after merging.
- **Paid runs are never triggered by merging.** Evaluations that cost money are run
  deliberately and their results committed in a PR of their own or with the change that
  motivated them.

## 8. What does not belong in a PR

- Reformatting of files the PR does not otherwise change.
- Dependency upgrades unrelated to the change.
- Result files from a run made with uncommitted code.
- Changes to the held-out split, the answers path handling or the spend limit, unless
  that is the PR's stated purpose.
