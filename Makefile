.PHONY: test lint format export smoke eval compare app

test:
	uv run --locked --group benchmark pytest $(PYTEST_ARGS)

lint:
	uv run --locked ruff check
	uv run --locked ruff format --check
	uv run --locked mypy

format:
	uv run --locked ruff format

export:
	uv run --locked --group benchmark rcaeval-agent export --dataset re2-ob --out agent_tasks

smoke:
	uv run --locked --group benchmark python -m scripts.benchmark_smoke

eval compare:
	@printf '%s\n' 'make $@ becomes available in Phase 06.' >&2
	@exit 1

app:
	@printf '%s\n' 'make app becomes available in Phase 08.' >&2
	@exit 1
