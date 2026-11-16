"""Tests for model, credential, and budget validation."""

import logging
from decimal import Decimal
from pathlib import Path

import pytest

from incidents_agent.llm import config


@pytest.mark.parametrize(
    ('cli', 'env', 'expected'),
    [
        (None, {}, '1.75'),
        (None, {config.BUDGET_ENV: '3'}, '2.75'),
        ('4', {config.BUDGET_ENV: '3'}, '3.75'),
    ],
)
def test_budget_uses_override_precedence(
    budget_file: Path, cli: str | None, env: dict[str, str], expected: str
) -> None:
    """Resolve CLI, environment, and file values in that order."""
    assert config.load_budget(
        budget_file, override=cli, environ=env
    ).limit == Decimal(expected)


@pytest.mark.parametrize('override', ['0.60', '-1', 'NaN', 'inf', 'not money'])
def test_invalid_budget_override_is_rejected(
    budget_file: Path, override: str
) -> None:
    """Reject invalid totals before opening a ledger."""
    with pytest.raises(config.ConfigError):
        config.load_budget(budget_file, override=override)


@pytest.mark.parametrize(
    'body',
    [
        '',
        'models: []\ndefault: llama',
        'models: {llama: {model: unsupported/foo}}\ndefault: llama',
        'models: {llama: {model: openai/foo}}\ndefault: other',
        'models: {llama: {model: openai/foo, price: 3}}\ndefault: llama',
        'models: {llama: {model: openai/foo, input_cost_per_mtok: 3}}\n'
        'default: llama',
        'models: {llama: {model: openai/foo, max_input_tokens: true}}\n'
        'default: llama',
        'models: {llama: {model: openai/foo, model: openai/bar}}\n'
        'default: llama',
    ],
)
def test_malformed_model_config_is_rejected(tmp_path: Path, body: str) -> None:
    """Reject malformed routing and price configuration."""
    path = tmp_path / 'models.yaml'
    path.write_text(body, encoding='utf-8')

    with pytest.raises(config.ConfigError):
        config.load_models(path)


def test_model_loader_recognizes_placeholders_and_versions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Distinguish setup placeholders and pinned IDs from aliases."""
    path = tmp_path / 'models.yaml'
    path.write_text(
        'models:\n'
        '  todo: {model: openai/<model-id>}\n'
        '  floating: {model: gemini/fake-2.5}\n'
        '  dated: {model: anthropic/fake-20250101}\n'
        'default: todo\n',
        encoding='utf-8',
    )

    with caplog.at_level(logging.WARNING):
        result = config.load_models(path)

    assert result.aliases[result.default].placeholder
    assert result.aliases['dated'].pinned
    assert not result.aliases['floating'].pinned
    assert 'floating ID gemini/fake' in caplog.text


def test_environment_prioritizes_process_values(tmp_path: Path) -> None:
    """Process credentials override dotenv credentials."""
    path = tmp_path / '.env'
    path.write_text(
        'OPENAI_API_KEY=file\nANTHROPIC_API_KEY=anthropic\n', encoding='utf-8'
    )

    values = config.environment(path, {'OPENAI_API_KEY': 'process'})

    assert values == {
        'OPENAI_API_KEY': 'process',
        'ANTHROPIC_API_KEY': 'anthropic',
    }


@pytest.mark.parametrize(
    'body',
    [
        'total_usd: 3\nsafety_margin_usd: 0\nother: 1',
        'total_usd: 3\nsafety_margin_usd: 0\nreserves: []',
        'total_usd: 3\nsafety_margin_usd: 0\nreserves: {"bad key": 1}',
        'total_usd: 3\nsafety_margin_usd: 0\nper_case_cap_usd: false',
        'total_usd: 3\nsafety_margin_usd: 4',
        'total_usd: 0.00000000000000000001\nsafety_margin_usd: 0',
    ],
)
def test_invalid_budget_file_is_rejected(tmp_path: Path, body: str) -> None:
    """Reject invalid fields, reserve names, and monetary values."""
    path = tmp_path / 'budget.yaml'
    path.write_text(body, encoding='utf-8')

    with pytest.raises(config.ConfigError):
        config.load_budget(path, environ={})
