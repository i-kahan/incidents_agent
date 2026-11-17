"""Render the Streamlit app headlessly over a saved run."""

from pathlib import Path

import pytest
from streamlit.testing import v1 as streamlit_testing

from app import runtime
from incidents_agent.agent import config as agent_config
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.agent import trajectory
from incidents_agent.llm import fake
from incidents_agent.memory import store as incident_memory
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


@pytest.mark.integration
def test_past_runs_tab_renders_multi_role_lanes_and_saves_feedback_to_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-agent lanes render and feedback updates MemoryStore."""
    runs_dir = tmp_path / 'runs'
    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE, case_id='victim_vs_cause'),
        fake.ScriptedClient(
            [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
                scripted.submit('inventory-db'),
            ]
        ),
        agent_config.AgentConfig(verify=False, memory=True),
        runs_dir=runs_dir,
        run_id='multi-run-1',
    )
    assert result.run_dir is not None
    rec = trajectory.Trajectory(
        result.run_dir / 'trajectory.jsonl', result.run_id
    )
    rec.record('model_call', role='lead', budget={'tool_calls': 1})
    rec.record(
        'tool_call',
        role='lead',
        node='lead',
        call_id='call_0001',
        tool='list_tables',
        args={},
        ok=True,
    )
    rec.record(
        'model_call',
        role='tester',
        hypothesis_id='H1',
        budget={'tool_calls': 2},
    )
    rec.record(
        'tool_call',
        role='tester',
        hypothesis_id='H1',
        node='testers',
        call_id='call_0002',
        tool='search_text',
        args={'pattern': 'Lock wait'},
        ok=True,
    )
    rec.record(
        'model_call',
        role='tester',
        hypothesis_id='H2',
        budget={'tool_calls': 3},
    )
    rec.record(
        'tool_call',
        role='tester',
        hypothesis_id='H2',
        node='testers',
        call_id='call_0003',
        tool='run_sql',
        args={'query': 'SELECT 1'},
        ok=True,
    )
    rec.record('model_call', role='skeptic', budget={'tool_calls': 4})
    rec.record(
        'verification',
        role='skeptic',
        hypothesis_id='H1',
        outcome='supported',
    )

    monkeypatch.setattr(runtime, 'DEFAULT_RUNS_DIR', runs_dir)
    monkeypatch.setattr(
        runtime, 'DEFAULT_MEMORY_PATH', runs_dir / 'memory.sqlite3'
    )

    app = streamlit_testing.AppTest.from_file(str(MAIN), default_timeout=30)
    app.run()

    assert not app.exception
    markdown_text = '\n'.join(item.value for item in app.markdown)
    assert 'Lead investigator' in markdown_text
    assert 'Tester (H1)' in markdown_text
    assert 'Tester (H2)' in markdown_text
    assert 'Independent skeptic' in markdown_text

    fb_radio = next(
        item for item in app.radio if item.label == 'Was the diagnosis correct?'
    )
    fb_radio.set_value('confirmed')
    fb_fix = next(
        item for item in app.text_input if item.label == 'What fixed it?'
    )
    fb_fix.set_value('Increased connection pool')
    save_btn = next(
        item for item in app.button if item.label == 'Save feedback'
    )
    save_btn.click()
    app.run()

    assert not app.exception
    mem_rec = incident_memory.MemoryStore(
        runs_dir / 'memory.sqlite3', frozen=True
    ).get('multi-run-1')
    assert mem_rec is not None
    assert mem_rec.outcome == 'confirmed'
    assert mem_rec.what_fixed_it == 'Increased connection pool'
