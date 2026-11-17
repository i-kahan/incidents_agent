"""Run entry point and manifest behaviour around the agent mode."""

from pathlib import Path

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.llm import fake
from tests.agent import scripted


def test_run_investigation_refuses_multi_until_built(tmp_path: Path) -> None:
    """Multi mode fails loudly instead of running the single graph."""
    with pytest.raises(ValueError, match='not built yet'):
        graph.run_investigation(
            state.Task(scripted.FIELD_CASE),
            fake.ScriptedClient([]),
            config.AgentConfig(mode='multi'),
            runs_dir=tmp_path,
        )


def test_single_run_manifest_records_its_mode(tmp_path: Path) -> None:
    """The manifest states which architecture produced the run."""
    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        fake.ScriptedClient([scripted.submit(outcome='no_fault_found')]),
        config.AgentConfig(verify=False),
        runs_dir=tmp_path,
    )

    start = result.events[0]
    manifest = start['manifest']

    assert start['type'] == 'run_start'
    assert isinstance(manifest, dict)
    assert manifest['mode'] == 'single'
