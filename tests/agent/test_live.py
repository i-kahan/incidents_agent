"""Opt-in agent run on one tuning case, billed through the shared ledger.

Runs only with `pytest -m live`, after the owner has filled in
`config/models.yaml` and `.env` and agreed to the spend. It uses the
cheapest usable configured model and asserts on shape, not content.
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from incidents_agent.agent import config as agent_config
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import litellm_transport
from scripts import budget
from scripts import check_models

_REPO = Path(__file__).resolve().parents[2]
_CASES = _REPO / 'agent_tasks' / 're2-ob' / 'cases'
_SPLIT = _REPO / 'splits' / 're2-ob.json'


def _cheapest_alias(guarded: client.GuardedClient) -> str | None:
    priced = []
    for alias in guarded.models.aliases:
        info = guarded.model_info(alias)
        if info.usable and info.price is not None:
            rates = info.price.tiers[0][1]
            priced.append((rates.input + rates.output, alias))
    return min(priced)[1] if priced else None


@pytest.mark.live
def test_live_agent_completes_one_tuning_case(tmp_path: Path) -> None:
    """One tuning case end to end on the cheapest configured model."""
    tuning = json.loads(_SPLIT.read_text(encoding='utf-8'))['tuning']
    case = _CASES / str(tuning[0])
    if not (case / 'task.json').is_file():
        pytest.skip('RE2-OB export not present; run make export')
    env = config.environment(Path('.env'))
    models = config.load_models(check_models.MODELS_FILE)
    guarded = client.GuardedClient(
        models,
        env,
        budget.open_ledger(None, env),
        litellm_transport.LiteLLMTransport(),
    )
    alias = _cheapest_alias(guarded)
    if alias is None:
        pytest.skip('No usable model configured')
    settings = agent_config.load_agent_config(_REPO / 'config' / 'agent.yaml')

    result = graph.run_investigation(
        state.Task.from_task_json(case / 'task.json'),
        guarded,
        agent_config.AgentConfig.from_dict(
            {**settings.to_dict(), 'model_alias': alias}
        ),
        runs_dir=tmp_path,
        run_id=f'live-agent-{uuid4().hex[:8]}',
    )

    assert result.report.outcome in (
        'verified',
        'provisional',
        'inconclusive',
        'no_fault_found',
    )
    assert any(event['type'] == 'model_call' for event in result.events)
    assert result.run_dir is not None
    assert (result.run_dir / 'report.md').is_file()
