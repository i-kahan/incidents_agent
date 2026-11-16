"""Tests for continuing a finished run with added files."""

import json
from pathlib import Path

import pytest

from incidents_agent.agent import config
from incidents_agent.agent import continuation
from incidents_agent.agent import graph
from incidents_agent.agent import state
from incidents_agent.llm import fake
from tests.agent import scripted

_SETTINGS = config.AgentConfig(verify=False)


def _first_pass(tmp_path: Path) -> graph.RunResult:
    return graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        fake.ScriptedClient(
            [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
                scripted.submit('inventory-db'),
            ]
        ),
        _SETTINGS,
        runs_dir=tmp_path / 'runs',
        run_id=scripted.RUN_ID,
    )


def _added_file(tmp_path: Path) -> Path:
    path = tmp_path / 'extra' / 'db_stats.csv'
    path.parent.mkdir()
    path.write_text(
        'time,inventory_db_deadlocks\n1700001000,0\n1700001210,14\n',
        encoding='utf-8',
    )
    return path


@pytest.mark.integration
def test_continuation_keeps_old_evidence_and_sees_added_table(
    tmp_path: Path,
) -> None:
    """Old citations stay valid and the added table is queryable."""
    first = _first_pass(tmp_path)
    assert first.run_dir is not None
    client = fake.ScriptedClient(
        [
            scripted.call('list_tables'),
            scripted.call(
                'update_hypotheses',
                operations=[
                    {
                        'op': 'revise',
                        'id': 'H1',
                        'evidence': [
                            scripted.LOCK_EVIDENCE,
                            {
                                'call_id': 'call_0002',
                                'locator': 'db_stats',
                                'quote': 'db_stats.csv',
                            },
                        ],
                    }
                ],
            ),
            scripted.submit('inventory-db'),
        ]
    )

    result = continuation.continue_run(
        first.run_dir, [_added_file(tmp_path)], client, _SETTINGS
    )

    ledger_event = [e for e in result.events if e['type'] == 'ledger'][-1]
    opening = str(client.requests[0][0].history[0].text)
    applied = ledger_event['result']
    assert isinstance(applied, dict)
    assert applied['errors'] == []
    assert 'New table db_stats from db_stats.csv' in opening
    assert (result.report.continuation, result.report.ranks) == (
        1,
        ('inventory-db',),
    )
    assert len(result.report.causes[0].evidence) == 2


@pytest.mark.integration
def test_continuation_reruns_verify_on_an_inconclusive_check(
    tmp_path: Path,
) -> None:
    """A cause left inconclusive can be verified once data is added."""
    settings = config.AgentConfig()
    verdict = {
        'hypothesis_id': 'H1',
        'check': 'is it upstream of the other affected components?',
    }
    first = graph.run_investigation(
        state.Task(scripted.FIELD_CASE),
        fake.ScriptedClient(
            [
                scripted.call('search_text', **scripted.LOCK_SEARCH),
                scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
                scripted.submit('inventory-db'),
                scripted.call(
                    graph.VERIFY_NAME, outcome='inconclusive', **verdict
                ),
            ]
        ),
        settings,
        runs_dir=tmp_path / 'runs',
        run_id=scripted.RUN_ID,
    )
    assert first.run_dir is not None
    assert first.report.outcome == 'provisional'

    result = continuation.continue_run(
        first.run_dir,
        [_added_file(tmp_path)],
        fake.ScriptedClient(
            [
                scripted.submit('inventory-db'),
                scripted.call('dependency_graph'),
                scripted.call(
                    graph.VERIFY_NAME,
                    outcome='supported',
                    evidence=[
                        {
                            'call_id': 'call_0002',
                            'locator': 'inventory-db',
                            'quote': 'order-service',
                        }
                    ],
                    **verdict,
                ),
            ]
        ),
        settings,
    )

    event = [e for e in result.events if e['type'] == 'continuation'][-1]
    assert event['reverify'] == ['H1']
    assert result.report.outcome == 'verified'
    assert result.report.causes[0].verification == 'supported'


@pytest.mark.integration
def test_continuation_appends_to_the_same_trajectory(tmp_path: Path) -> None:
    """Both passes are in one trajectory, tagged by continuation index."""
    first = _first_pass(tmp_path)
    assert first.run_dir is not None

    continuation.continue_run(
        first.run_dir,
        [_added_file(tmp_path)],
        fake.ScriptedClient([scripted.submit('inventory-db')]),
        _SETTINGS,
    )

    lines = (first.run_dir / 'trajectory.jsonl').read_text().splitlines()
    indexes = {json.loads(line)['continuation'] for line in lines}
    assert indexes == {0, 1}
    assert (first.run_dir / 'report-c1.json').exists()


@pytest.mark.integration
def test_continuation_refuses_a_name_already_in_the_run(
    tmp_path: Path,
) -> None:
    """Adding a file whose name is already in the run is refused."""
    first = _first_pass(tmp_path)
    assert first.run_dir is not None
    clash = tmp_path / 'other' / 'metrics.csv'
    clash.parent.mkdir()
    clash.write_text('time,x\n1,2\n', encoding='utf-8')

    with pytest.raises(ValueError, match='already in the run'):
        continuation.continue_run(
            first.run_dir, [clash], fake.ScriptedClient([]), _SETTINGS
        )


def test_continue_run_without_checkpoint_raises(tmp_path: Path) -> None:
    """A folder that holds no run cannot be continued."""
    with pytest.raises(FileNotFoundError):
        continuation.continue_run(
            tmp_path, [], fake.ScriptedClient([]), _SETTINGS
        )


def test_what_changed_lists_new_and_changed_tables() -> None:
    """New tables, changed row counts and new documents are each listed."""
    before = {
        'tables': {'m': {'source_file': 'm.csv', 'row_count': 1, 'columns': 2}},
        'documents': [],
    }
    after = {
        'tables': {
            'm': {'source_file': 'm.csv', 'row_count': 5, 'columns': 2},
            'n': {'source_file': 'n.csv', 'row_count': 3, 'columns': 4},
        },
        'documents': ['notes.md'],
    }

    lines = continuation.what_changed(before, after)

    assert lines == [
        '- Table m changed: 1 -> 5 rows',
        '- New table n from n.csv: 3 rows, 4 columns',
        '- New document notes.md',
    ]
