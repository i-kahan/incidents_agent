"""Human audit integrity, onset denominators and resolved selection gates."""

import copy

import pytest

from incidents_agent.eval import audit
from incidents_agent.eval import onset
from incidents_agent.eval import selection
from incidents_agent.eval import split


@pytest.fixture
def package():
    """An unsigned 15-report audit."""
    report = {
        'causes': [{'statement': 'Claim', 'evidence': [{'quote': 'value'}]}],
        'actions': [{'action': 'Check', 'risk': 'read_only'}],
    }
    reports = {f'c{i:02}': report for i in range(20)}
    return audit.prepare(reports, tuple(reports))


def test_audit_pending_judgments_are_not_zeros(package):
    """Audit pending judgments are not zeros."""
    summary = audit.summarize(package, package)

    assert isinstance(summary['support'], dict)
    assert summary['support']['completed'] == 0
    assert isinstance(summary['support'], dict)
    assert summary['support']['rate'] is None


def test_audit_only_completed_judgments_count(package):
    """Audit only completed judgments count."""
    completed = copy.deepcopy(package)
    completed['reports'][0]['claims'][0]['supports_claim'] = True
    completed['reports'][0]['actions'][0]['judgments']['specific'] = False

    summary = audit.summarize(package, completed)

    assert isinstance(summary['support'], dict)
    assert summary['support']['rate'] == 1
    assert isinstance(summary['support'], dict)
    assert summary['support']['completed'] == 1
    assert isinstance(summary['actions'], dict)
    assert summary['actions']['specific']['rate'] == 0


def test_audit_rejects_changed_source_hash(package):
    """Audit rejects changed source hash."""
    altered = copy.deepcopy(package)
    altered['reports'][0]['report_sha256'] = 'changed'

    with pytest.raises(ValueError, match='Frozen'):
        audit.summarize(package, altered)


def test_audit_rejects_small_sample():
    """Audit rejects small sample."""
    with pytest.raises(ValueError, match='15'):
        audit.prepare({}, ())


def test_onset_missing_estimate_stays_in_denominator():
    """Onset missing estimate stays in denominator."""
    summary = onset.summarize({'a': 10, 'b': 100, 'c': None})

    assert summary['detection_coverage'] == pytest.approx(2 / 3)
    assert summary['within_60_seconds'] == pytest.approx(1 / 3)
    assert summary['median_absolute_error_s'] == 55
    assert summary['p90_absolute_error_s'] == 91


@pytest.fixture
def partition():
    """A locked split with opaque group membership."""
    return split.Split(
        'toy', 6, 1, 'hash', {'g1': ('a',), 'g2': ('b',)}, ('g1',), ('g2',)
    )


@pytest.mark.parametrize('selector', ['all', 'heldout', 'b', 'a,b'])
def test_heldout_gate_resolves_all_routes(partition, selector):
    """Heldout gate resolves all routes."""
    with pytest.raises(ValueError, match='--final'):
        selection.resolve(selector, partition)


def test_gate_uses_resolved_limited_cases(partition):
    """Gate uses resolved limited cases."""
    assert selection.resolve('all', partition, limit=1) == ('a',)


def test_final_allows_heldout(partition):
    """Final allows heldout."""
    assert selection.resolve('b', partition, final=True) == ('b',)


@pytest.mark.integration
def test_onset_evaluate_real_fixture(tmp_path):
    """A known shift is evaluated using the real ingest/tool boundary."""
    folder = tmp_path / 'input'
    folder.mkdir()
    (folder / 'metrics.csv').write_text(
        'time,cpu\n'
        + ''.join(
            f'{1700000000 + i * 10},{10 + i % 3 + 100 * (i >= 30)}\n'
            for i in range(60)
        )
    )

    result = onset.evaluate({'toy': (folder, 1700000300.0)})

    assert result['n_cases'] == 1
    assert result['n_detected'] == 1
    assert result['within_60_seconds'] == 1


def test_onset_evaluate_failure_is_not_dropped(tmp_path):
    """Missing inputs stay in the intended denominator."""
    result = onset.evaluate({'missing': (tmp_path / 'missing', 1.0)})

    assert result['n_cases'] == 1
    assert result['within_60_seconds'] == 0


@pytest.mark.parametrize(
    'selector,limit', [('unknown', None), ('a,a', None), ('tuning', 0)]
)
def test_selection_invalid_inputs_rejected(partition, selector, limit):
    """Unknown IDs, duplicates and invalid limits fail before execution."""
    with pytest.raises(ValueError):
        selection.resolve(selector, partition, limit=limit)


def test_field_selection_is_separate(partition):
    """Field case IDs come from the field inventory, not benchmark labels."""
    assert selection.resolve('field', partition, field_ids=('healthy',)) == (
        'healthy',
    )
