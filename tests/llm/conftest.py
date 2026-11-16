"""Small offline model layer fixtures."""

from pathlib import Path

import pytest

from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger


@pytest.fixture
def budget_file(tmp_path: Path) -> Path:
    """Write a budget with room for several small requests."""
    path = tmp_path / 'budget.yaml'
    path.write_text(
        'total_usd: 2\nsafety_margin_usd: 0.25\n'
        'per_case_cap_usd: 0.75\nreserves:\n  final: 0.50\n',
        encoding='utf-8',
    )
    return path


@pytest.fixture
def spend(tmp_path: Path, budget_file: Path) -> ledger.SpendLedger:
    """Open an isolated ledger for offline calls."""
    return ledger.SpendLedger(tmp_path / 'spend.sqlite', budget_file)


@pytest.fixture
def models() -> config.Models:
    """Use an artificial pinned Anthropic model."""
    spec = config.ModelSpec('small', 'anthropic/fake-20250101', 'anthropic')
    return config.Models({'small': spec}, 'small')


@pytest.fixture
def rate_card() -> dict[str, object]:
    """Supply known rates including cache reads and writes."""
    return {
        'fake-20250101': {
            'litellm_provider': 'anthropic',
            'max_input_tokens': 100,
            'max_output_tokens': 100,
            'input_cost_per_token': '0.001',
            'output_cost_per_token': '0.002',
            'cache_read_input_token_cost': '0.0001',
            'cache_creation_input_token_cost': '0.00125',
            'supports_prompt_caching': True,
        },
        'fake-20250202': {
            'litellm_provider': 'anthropic',
            'max_input_tokens': 100,
            'input_cost_per_token': '0.001',
            'output_cost_per_token': '0.003',
        },
    }


@pytest.fixture
def request_data() -> contracts.ChatRequest:
    """A text prompt with an ordered tool and catalog briefing."""
    return contracts.ChatRequest(
        '',
        'Investigate.',
        10,
        tools=(contracts.Tool('read_data', 'Read data', {'type': 'object'}),),
        catalog_briefing='Catalog',
        history=(contracts.Message('user', 'Start'),),
    )
