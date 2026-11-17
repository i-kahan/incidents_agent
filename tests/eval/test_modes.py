"""Agent modes in evaluation: --mode, mode arms, selection, role report."""

import argparse
import csv
import dataclasses
import pathlib

import pytest

from incidents_agent.eval import comparison
from incidents_agent.eval import failures
from incidents_agent.eval import runner
from incidents_agent.eval import scoring
from incidents_agent.eval import service


def _agent(
    options: service.Options, mode: str | None = None
) -> service.Options:
    """Return the fixture's options for the agent, not single-call."""
    return dataclasses.replace(options, agent='single', mode=mode)


def _usage(record: dict[str, object]) -> dict[str, object]:
    usage = record['usage']
    assert isinstance(usage, dict)
    return usage


def _parse(*argv: str) -> service.Options:
    parser = argparse.ArgumentParser()
    service.add_arguments(parser)
    return service.Options.from_args(parser.parse_args(list(argv)))


def test_mode_option_reaches_options() -> None:
    """`--mode multi` is a shared evaluation option."""
    assert _parse('--mode', 'multi').mode == 'multi'
    assert _parse().mode is None


def test_unknown_mode_is_refused_by_the_parser() -> None:
    """Only the built architectures can be named."""
    with pytest.raises(SystemExit):
        _parse('--mode', 'swarm')


def test_mode_sets_agent_config_and_changes_the_cache_key(
    evaluation_options: service.Options,
) -> None:
    """The mode is in the frozen configuration, so caches never mix."""
    single = service.prepare(_agent(evaluation_options))
    multi = service.prepare(_agent(evaluation_options, 'multi'))
    cases = multi.manifest['cases']
    assert isinstance(cases, list)
    case = cases[0]

    assert comparison.arm_mode(single) == 'single'
    assert comparison.arm_mode(multi) == 'multi'
    assert runner.cache_key(single.manifest, case) != runner.cache_key(
        multi.manifest, case
    )


def test_single_call_baseline_refuses_multi_mode(
    evaluation_options: service.Options,
) -> None:
    """The single-call baseline has no roles to split into."""
    with pytest.raises(ValueError, match='no multi mode'):
        service.prepare(
            dataclasses.replace(
                evaluation_options, agent='single_call', mode='multi'
            )
        )


@pytest.mark.integration
def test_multi_mode_dry_run_scores_and_reports_roles(
    evaluation_options: service.Options,
) -> None:
    """A free multi-mode run scores every case and records each role."""
    result = service.execute(
        service.prepare(_agent(evaluation_options, 'multi'))
    )
    usage = result.records[0]['usage']

    assert result.summary['n_predicted'] == 3
    assert isinstance(usage, dict)
    assert {'lead', 'tester', 'skeptic'} <= set(usage['by_role'])
    assert all(
        float(str(_usage(record)['tool_calls'])) <= 15
        for record in result.records
    )

    folder, _ = failures.run(result.folder)
    report = runner.read_object(folder / 'failure-report.json')

    assert report['skeptic'] == {
        'challenged': 3,
        'disproved': 0,
        'disproved_right': 0,
        'disproved_wrong': 0,
    }
    by_role = report['by_role']
    assert isinstance(by_role, dict)
    assert by_role['tester_tool_calls'] > 0


@pytest.mark.integration
def test_single_mode_failure_report_has_no_role_columns(
    evaluation_options: service.Options,
) -> None:
    """Single-mode reports keep their Phase 06 columns."""
    result = service.execute(service.prepare(_agent(evaluation_options)))

    folder, _ = failures.run(result.folder)
    report = runner.read_object(folder / 'failure-report.json')
    cases = report['cases']

    assert 'by_role' not in report
    assert isinstance(cases, list)
    assert 'skeptic_outcome' not in cases[0]


@pytest.mark.integration
def test_compare_modes_side_by_side_with_selection(
    tmp_path: pathlib.Path, evaluation_options: service.Options
) -> None:
    """Single and multi arms share a table and get a selection block."""
    folder, table = comparison.run(
        _agent(evaluation_options),
        ['haiku'],
        modes=['single', 'multi'],
        results_root=tmp_path / 'comparison',
    )
    saved = runner.read_object(folder / 'comparison.json')
    with (folder / 'comparison.csv').open() as stream:
        rows = list(csv.DictReader(stream))

    assert [(row['model'], row['mode']) for row in rows] == [
        ('baro', 'baseline'),
        ('guarded_baro', 'baseline'),
        ('haiku', 'single'),
        ('haiku', 'multi'),
    ]
    arms = saved['models']
    assert isinstance(arms, dict)
    assert set(arms) == {'haiku:single', 'haiku:multi'}
    selection = saved['selection']
    assert isinstance(selection, dict)
    assert selection['haiku']['selected'] in ('single', 'multi')
    assert 'selected:' in table


def test_duplicate_modes_fail_before_execution(
    evaluation_options: service.Options,
) -> None:
    """A mode listed twice cannot start a comparison."""
    with pytest.raises(ValueError, match='Modes'):
        comparison.run(
            _agent(evaluation_options), ['haiku'], modes=['multi', 'multi']
        )


def _case(
    case_id: str, service_name: str, rank: int | None
) -> scoring.CaseScore:
    return scoring.CaseScore(
        case_id, service_name, 'cpu', None, rank, None, None
    )


def _scored(ranks: list[int | None]) -> scoring.ScoredRun:
    names = ['svc-a', 'svc-b', 'svc-c', 'svc-d']
    return scoring.ScoredRun(
        {},
        tuple(
            _case(f'c{n}', names[n % 4], rank) for n, rank in enumerate(ranks)
        ),
    )


@pytest.mark.parametrize(
    ('multi', 'single', 'selected'),
    [
        pytest.param(
            [1] * 8, [None] * 8, 'multi', id='clear_gain_lower_bound_above_0'
        ),
        pytest.param([1] * 8, [1] * 8, 'single', id='no_gain'),
        pytest.param(
            [1, None] * 4, [None, 1] * 4, 'single', id='mixed_interval_spans_0'
        ),
    ],
)
def test_selection_rule_needs_lower_bound_above_zero(
    multi: list[int | None], single: list[int | None], selected: str
) -> None:
    """Multi is chosen only when its whole interval is a gain."""
    chosen = comparison.select_mode(_scored(multi), _scored(single))

    assert chosen['selected'] == selected


def test_selection_with_one_group_has_no_interval_and_keeps_single() -> None:
    """Without two service-fault pairs there is no interval, so single."""
    one = scoring.ScoredRun({}, (_case('c1', 'svc-a', 1),))
    none = scoring.ScoredRun({}, (_case('c1', 'svc-a', None),))

    chosen = comparison.select_mode(one, none)

    assert (chosen['low'], chosen['selected']) == (None, 'single')


@pytest.mark.parametrize(
    ('challenge', 'expected'),
    [
        pytest.param(
            {'outcome': 'disproved', 'component': 'svc-b'}, True, id='right'
        ),
        pytest.param(
            {'outcome': 'disproved', 'component': 'SVC-A'}, False, id='wrong'
        ),
        pytest.param(
            {'outcome': 'holds', 'component': 'svc-a'}, None, id='held'
        ),
    ],
)
def test_skeptic_overturn_judged_against_the_true_service(
    challenge: dict[str, object], expected: bool | None
) -> None:
    """A disproval is right only when it overturned a wrong answer."""
    columns = failures.skeptic_columns({'skeptic': challenge}, 'svc-a')

    assert columns['skeptic_overturn_right'] is expected
