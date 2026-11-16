"""Walking-skeleton tests: rendering, the rule-based fake and the scripts."""

import dataclasses
import sys
from pathlib import Path

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import fake_policy
from incidents_agent.agent import graph
from incidents_agent.agent import render
from incidents_agent.agent import report
from incidents_agent.agent import state
from incidents_agent.llm import config as llm_config
from scripts import check_models
from scripts import investigate as investigate_script
from scripts import replay
from tests.agent import scripted


def _report(
    outcome: report.Outcome = 'inconclusive',
    causes: tuple[report.RankedCause, ...] = (),
) -> report.Report:
    base = report.Report(
        run_id='r',
        continuation=0,
        outcome='inconclusive',
        stop_reason='tool_call_limit',
        fault_type=None,
        ranks=(),
        causes=(),
        actions=(),
        data_requests=(),
        dropped=('rank x: no valid supporting citation',),
        counters={'tool_calls': 15, 'model_turns': 9, 'cost_usd': '0.01'},
        prompt_hash='abc',
    )
    return dataclasses.replace(
        base,
        outcome=outcome,
        causes=causes,
        ranks=tuple(cause.component for cause in causes),
    )


def test_render_inconclusive_report_lists_dropped_claims() -> None:
    """An inconclusive report says so and lists what validation dropped."""
    text = render.render(_report())

    assert '**Outcome:** Inconclusive' in text
    assert 'No cause is ranked.' in text
    assert '- rank x: no valid supporting citation' in text


def test_render_names_the_tool_behind_each_citation() -> None:
    """Citations show the call id, its tool, the locator and the quote."""
    cause = report.RankedCause(
        1,
        'db',
        'db locks',
        'H1',
        'supported',
        'high',
        ('1 valid supporting citation(s)',),
        (report.EvidenceRef('call_0003', 'service', 'Lock wait'),),
    )

    text = render.render(
        _report('verified', (cause,)),
        {'call_0003': 'search_text'},
    )

    assert '- [for] `call_0003` search_text, at `service`: "Lock wait"' in (
        text
    )


@pytest.mark.parametrize(
    ('column', 'candidates', 'expected'),
    [
        pytest.param(
            'checkoutservice_cpu',
            ('cartservice', 'checkoutservice'),
            'checkoutservice',
            id='candidate',
        ),
        pytest.param(
            'api_gateway_queue_depth', (), 'api-gateway', id='suffixes'
        ),
        pytest.param('latency', (), 'latency', id='single_token'),
    ],
)
def test_component_from_column(
    column: str, candidates: tuple[str, ...], expected: str
) -> None:
    """Columns map to a candidate, or to their name without metric suffixes."""
    assert fake_policy.component_from_column(column, candidates) == expected


@pytest.mark.integration
def test_rule_client_runs_the_whole_graph(tmp_path: Path) -> None:
    """The rule-based fake reaches every node and cites a real value."""
    result = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        fake_policy.RuleClient(),
        config.AgentConfig(),
        runs_dir=tmp_path,
    )

    nodes = [e['node'] for e in result.events if e['type'] == 'node']
    assert nodes == ['orient', 'investigate', 'verify', 'report']
    assert result.report.causes[0].verification == 'inconclusive'
    assert len(result.report.causes[0].evidence) == 1


@pytest.mark.integration
def test_investigate_and_replay_scripts_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`investigate.py --fake` writes a report that `replay.py` reproduces."""
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'investigate.py',
            str(scripted.FIELD_CASE),
            '--fake',
            '--runs-dir',
            str(tmp_path / 'runs'),
        ],
    )
    assert investigate_script.main() == 0
    run_dir = next((tmp_path / 'runs').iterdir())

    markdown, valid, total = replay.replay(run_dir / 'trajectory.jsonl')

    assert markdown == (run_dir / 'report.md').read_text(encoding='utf-8')
    assert (valid, total) == (1, 1)


@pytest.mark.integration
def test_investigate_script_continues_a_run_with_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--continue-run` with `--add` writes the next continuation."""
    first = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        fake_policy.RuleClient(),
        config.AgentConfig(),
        runs_dir=tmp_path,
    )
    assert first.run_dir is not None
    added = tmp_path / 'notes.md'
    added.write_text('# Notes\n\nDeploy at 12:00.\n', encoding='utf-8')
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'investigate.py',
            '--fake',
            '--continue-run',
            str(first.run_dir),
            '--add',
            str(added),
        ],
    )

    assert investigate_script.main() == 0
    assert (first.run_dir / 'report-c1.md').exists()


def test_investigate_script_refuses_unusable_model_before_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --fake a placeholder model stops the run before any call."""
    models = tmp_path / 'models.yaml'
    models.write_text(
        'models:\n  small:\n    model: anthropic/<model-id>\ndefault: small\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(check_models, 'MODELS_FILE', models)
    monkeypatch.setattr(llm_config, 'environment', lambda path: {})
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'investigate.py',
            str(scripted.FIELD_CASE),
            '--runs-dir',
            str(tmp_path / 'runs'),
        ],
    )

    with pytest.raises(SystemExit):
        investigate_script.main()

    assert not (tmp_path / 'runs').exists()
