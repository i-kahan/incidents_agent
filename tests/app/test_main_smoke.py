"""Render the Streamlit app headlessly over a saved run."""

from pathlib import Path

import pytest
from streamlit.testing import v1 as streamlit_testing

from app import runtime
from incidents_agent.agent import config as agent_config
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.llm import fake
from tests.agent import scripted

MAIN = Path(__file__).resolve().parents[2] / 'app' / 'main.py'


@pytest.mark.integration
def test_past_runs_tab_renders_report_and_continuation_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved run opens with its continuation and feedback sections."""
    runs_dir = tmp_path / 'runs'
    graph.run_investigation(
        state.Task(scripted.FIELD_CASE, case_id='victim_vs_cause'),
        fake.ScriptedClient(
            [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
                scripted.submit('inventory-db'),
            ]
        ),
        agent_config.AgentConfig(verify=False),
        runs_dir=runs_dir,
    )
    monkeypatch.setattr(runtime, 'DEFAULT_RUNS_DIR', runs_dir)

    app = streamlit_testing.AppTest.from_file(str(MAIN), default_timeout=30)
    app.run()

    assert not app.exception
    assert any('Saved trajectory' in box.label for box in app.selectbox)
    labels = {item.label for item in app.expander}
    assert 'Add files and continue' in labels
    assert 'Delete this run' in labels
