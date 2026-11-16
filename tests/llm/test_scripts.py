"""Exercise the Phase 4 command entry points without provider network access."""

import json
import sys
from pathlib import Path

import pytest

from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger
from incidents_agent.llm import litellm_transport
from incidents_agent.llm import pricing
from scripts import budget
from scripts import check_models


class _Probe:
    def input_bound(
        self, spec: config.ModelSpec, request: contracts.ChatRequest
    ) -> int | None:
        return 20

    def complete(
        self,
        spec: config.ModelSpec,
        request: contracts.ChatRequest,
        *,
        api_key: str,
        cache: bool,
        timeout: float,
    ) -> contracts.ProviderResult:
        return contracts.ProviderResult(
            contracts.Message('assistant', 'OK'),
            contracts.Usage(20, 1),
            spec.id,
            'test-request',
        )


@pytest.fixture
def local_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_file: Path,
    rate_card: dict[str, object],
) -> Path:
    """Point the commands at synthetic model config and an isolated ledger."""
    models_file = tmp_path / 'models.yaml'
    models_file.write_text(
        'models: {demo: {model: anthropic/fake-20250101}}\ndefault: demo\n'
    )
    allocation_file = tmp_path / 'allocation.yaml'
    allocation_file.write_text('Final: 1\nPilot: 1\nOptional: null\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ledger, 'DEFAULT_PATH', tmp_path / 'runs' / 'spend.db')
    monkeypatch.setattr(budget, 'BUDGET_FILE', budget_file)
    monkeypatch.setattr(budget, 'ALLOCATION_FILE', allocation_file)
    monkeypatch.setattr(check_models, 'MODELS_FILE', models_file)
    monkeypatch.setattr(pricing, 'bundled_catalog', lambda: rate_card)
    monkeypatch.setattr(
        config,
        'environment',
        lambda _: {'ANTHROPIC_API_KEY': 'test-key'},
    )
    return tmp_path / 'runs'


def test_model_inventory_is_free_by_default(
    local_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show model diagnostics without opening a spend ledger."""
    monkeypatch.setattr(sys, 'argv', ['check_models'])

    assert check_models.main() == 0

    output = capsys.readouterr().out
    assert 'demo (anthropic/fake-20250101): usable; pinned' in output
    assert 'price per million tokens:' in output
    assert not local_cli.exists()


def test_live_cli_persists_budget_and_provider_metadata(
    local_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise the explicit paid route with a fake transport."""
    monkeypatch.setattr(sys, 'argv', ['check_models', '--live'])
    monkeypatch.setattr(litellm_transport, 'LiteLLMTransport', _Probe)

    assert check_models.main() == 0

    (manifest_path,) = local_cli.glob('*/manifest.json')
    manifest = json.loads(manifest_path.read_text())
    record = json.loads(manifest_path.with_name('trajectory.jsonl').read_text())
    assert manifest['limit_usd'] == record['limit_usd'] == '1.75'
    assert manifest['counters']['attempts'] == 1
    assert record['request_id'] == 'test-request'
    assert 'provider served anthropic/fake-20250101' in capsys.readouterr().out


def test_budget_cli_scales_display_without_changing_reserves(
    local_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scale allocation weights against the invocation limit."""
    monkeypatch.setattr(sys, 'argv', ['budget', '--budget-usd', '4.25'])

    assert budget.main() == 0

    output = capsys.readouterr().out
    assert 'Limit:     $4.00' in output
    assert 'Reserve final: $0.5 remaining' in output
    assert 'Pilot' in output and '2.00' in output
    assert 'from savings' in output
