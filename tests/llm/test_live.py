"""Opt-in production provider checks, billed through the shared ledger."""

from pathlib import Path
from uuid import uuid4

import pytest

from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import litellm_transport
from scripts import budget
from scripts import check_models


@pytest.mark.live
@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini'])
def test_live_provider_reports_usage(provider: str) -> None:
    """Check real provider usage through the shared production ledger."""
    env = config.environment(Path('.env'))
    models = config.load_models(check_models.MODELS_FILE)
    spend = budget.open_ledger(None, env)
    llm = client.GuardedClient(
        models,
        env,
        spend,
        litellm_transport.LiteLLMTransport(),
        policy=client.RetryPolicy(attempts=1),
    )
    aliases = [
        alias
        for alias, spec in models.aliases.items()
        if spec.provider == provider and llm.model_info(alias).usable
    ]
    if not aliases:
        pytest.skip(f'No usable {provider} model configured')

    result = llm.complete(
        check_models.probe_request(aliases[0]),
        run=contracts.Run(f'live-test-{provider}-{uuid4().hex}'),
    )

    assert result.response.served_model
    assert result.response.usage.input_tokens > 0
    assert result.response.message.role == 'assistant'
