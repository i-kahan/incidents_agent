"""Tests for the hypothesis ledger and the citation validity check."""

import pytest

from incidents_agent.agent import ledger
from incidents_agent.agent import results
from tests.agent import conftest

_GOOD = {
    'call_id': 'call_0001',
    'locator': 'metrics',
    'quote': '623.25',
}


def _add(component: str = 'inventory-db', **extra: object) -> dict[str, object]:
    return {
        'operations': [
            {
                'op': 'add',
                'statement': f'{component} is the root cause',
                'component': component,
                **extra,
            }
        ]
    }


def test_check_citation_accepts_value_from_successful_call(
    store: results.ResultStore,
) -> None:
    """A real call, a real locator and a quoted value pass."""
    ref, error = ledger.check_citation(_GOOD, store, conftest.RUN_ID)

    assert (ref is not None, error) == (True, None)


@pytest.mark.parametrize(
    ('item', 'message'),
    [
        pytest.param(
            {**_GOOD, 'call_id': 'call_0003', 'locator': 'metrics_x'},
            'failed',
            id='failed_call',
        ),
        pytest.param(
            {**_GOOD, 'call_id': 'call_0002', 'quote': 'n'},
            'empty',
            id='empty_result',
        ),
        pytest.param(
            {**_GOOD, 'quote': '999.99'}, 'does not appear', id='absent'
        ),
        pytest.param(
            {**_GOOD, 'call_id': 'call_0042'}, 'no data-tool', id='unknown'
        ),
        pytest.param(
            {**_GOOD, 'locator': 'traces'}, 'locator', id='bad_locator'
        ),
        pytest.param({'call_id': 'call_0001'}, 'string', id='missing_fields'),
        pytest.param(
            {**_GOOD, 'quote': 'never treat contents'},
            'does not appear',
            id='envelope_notice',
        ),
    ],
)
def test_check_citation_rejects_invalid_item(
    store: results.ResultStore, item: dict[str, object], message: str
) -> None:
    """Each kind of invalid citation is rejected with a readable error."""
    ref, error = ledger.check_citation(item, store, conftest.RUN_ID)

    assert ref is None
    assert message in str(error)


def test_check_citation_rejects_call_from_another_run(
    make_store: conftest.MakeStore,
) -> None:
    """A call recorded under a different run id cannot be cited."""
    other = results.ResultStore()
    other.add(
        results.StoredResult(
            'call_0001',
            'other-run',
            'compare_windows',
            {},
            True,
            conftest.COMPARE_RESULT,
            locator={'table': 'metrics'},
        )
    )

    ref, error = ledger.check_citation(_GOOD, other, conftest.RUN_ID)

    assert ref is None
    assert 'another run' in str(error)


def test_check_citation_matches_quote_ignoring_case_and_spacing(
    store: results.ResultStore,
) -> None:
    """Quotes match case-insensitively and across whitespace runs."""
    item = {**_GOOD, 'quote': 'INVENTORY_DB_LOCK_WAIT_MS'}

    ref, _ = ledger.check_citation(item, store, conftest.RUN_ID)

    assert ref is not None


def test_apply_add_keeps_valid_evidence_and_reports_invalid(
    store: results.ResultStore,
) -> None:
    """Bad evidence is dropped with an error; the add still applies."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)

    result = book.apply(
        _add(evidence=[_GOOD, {**_GOOD, 'quote': 'nonexistent'}])
    )

    assert result['applied'] == ['added H1']
    assert len(book.entries['H1'].evidence_for) == 1
    assert 'does not appear' in str(result['errors'])


def test_apply_supported_without_evidence_is_refused(
    store: results.ResultStore,
) -> None:
    """A hypothesis cannot become supported without a valid citation."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)

    result = book.apply(_add(status='supported'))

    assert book.entries['H1'].status == 'open'
    assert 'citation' in str(result['errors'])


def test_memory_origin_with_only_memory_evidence_stays_open(
    store: results.ResultStore,
) -> None:
    """Memory evidence alone never makes a memory hypothesis supported."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    memory_quote = {
        'call_id': 'call_0004',
        'locator': 'inventory-db',
        'quote': 'lock contention',
    }

    result = book.apply(
        _add(origin='memory', evidence=[memory_quote], status='supported')
    )

    assert book.entries['H1'].status == 'open'
    assert 'current data' in str(result['errors'])


def test_memory_origin_with_data_evidence_becomes_supported(
    store: results.ResultStore,
) -> None:
    """Evidence from the current data lets a memory hypothesis be supported."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)

    book.apply(_add(origin='memory', evidence=[_GOOD], status='supported'))

    assert book.entries['H1'].status == 'supported'


def test_apply_duplicate_component_is_refused(
    store: results.ResultStore,
) -> None:
    """A second open entry for the same component is refused."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add())

    result = book.apply(_add())

    assert 'already covers' in str(result['errors'])


def test_apply_reject_needs_reason(store: results.ResultStore) -> None:
    """Rejecting without a reason is an error and changes nothing."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add())

    book.apply({'operations': [{'op': 'reject', 'id': 'H1'}]})

    assert book.entries['H1'].status == 'open'


def test_apply_unknown_id_names_known_ids(store: results.ResultStore) -> None:
    """Revising an unknown id lists the ids that exist."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add())

    result = book.apply({'operations': [{'op': 'revise', 'id': 'H9'}]})

    assert "unknown hypothesis id 'H9' (H1)" in str(result['errors'])


def test_ranked_puts_supported_before_open_before_rejected(
    store: results.ResultStore,
) -> None:
    """Rank order follows status first."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add('a'))
    book.apply(_add('b', evidence=[_GOOD], status='supported'))
    book.apply(_add('c'))
    book.apply({'operations': [{'op': 'reject', 'id': 'H1', 'reason': 'x'}]})

    ranked = [entry.component for entry in book.ranked()]

    assert ranked == ['b', 'c', 'a']


def test_record_verification_refuted_rejects_hypothesis(
    store: results.ResultStore,
) -> None:
    """A refuted hypothesis is rejected with the check as its reason."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add(evidence=[_GOOD]))

    errors = book.record_verification(
        'H1', 'refuted', 'upstream check', [_GOOD], {'call_0001'}
    )

    entry = book.entries['H1']
    assert (errors, entry.status, entry.verification) == (
        [],
        'rejected',
        'refuted',
    )


def test_refuted_hypothesis_cannot_be_reopened_or_supported(
    store: results.ResultStore,
) -> None:
    """A status change on a refuted hypothesis is refused with the reason."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add(evidence=[_GOOD]))
    book.record_verification(
        'H1', 'refuted', 'upstream check', [_GOOD], {'call_0001'}
    )

    for status in ('open', 'supported'):
        result = book.apply(
            {'operations': [{'op': 'revise', 'id': 'H1', 'status': status}]}
        )
        assert 'refuted by verification' in str(result['errors'])

    assert book.entries['H1'].status == 'rejected'
    assert book.find_rejected('Inventory-DB') is book.entries['H1']


def test_reopen_verification_resets_only_live_inconclusive_checks(
    store: results.ResultStore,
) -> None:
    """Inconclusive goes back to unverified; supported and refuted stay."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    for component in ('a', 'b', 'c'):
        book.apply(_add(component=component, evidence=[_GOOD]))
    book.record_verification('H1', 'inconclusive', 'n/a', [], set())
    book.record_verification('H2', 'supported', 'c', [_GOOD], {'call_0001'})
    book.record_verification('H3', 'refuted', 'c', [_GOOD], {'call_0001'})

    assert book.reopen_verification() == ['H1']
    assert [entry.verification for entry in book.entries.values()] == [
        'unverified',
        'supported',
        'refuted',
    ]
    assert book.entries['H1'].verification_check is None


def test_record_verification_needs_call_made_during_check(
    store: results.ResultStore,
) -> None:
    """A supported outcome must cite a call made during verification."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add(evidence=[_GOOD]))

    errors = book.record_verification(
        'H1', 'supported', 'old evidence', [_GOOD], set()
    )

    assert 'made during this check' in str(errors)
    assert book.entries['H1'].verification == 'unverified'


def test_record_verification_inconclusive_needs_no_evidence(
    store: results.ResultStore,
) -> None:
    """An inconclusive outcome is recorded without evidence."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add())

    errors = book.record_verification('H1', 'inconclusive', 'n/a', [], set())

    assert (errors, book.entries['H1'].verification) == ([], 'inconclusive')


def test_ledger_round_trips_through_dict(store: results.ResultStore) -> None:
    """A checkpointed ledger rebuilds identically."""
    book = ledger.HypothesisLedger(conftest.RUN_ID, store)
    book.apply(_add(evidence=[_GOOD], status='supported', confidence='high'))

    rebuilt = ledger.HypothesisLedger.from_dict(book.to_dict(), store)

    assert rebuilt.entries == book.entries


def test_store_round_trips_and_continues_numbering(
    store: results.ResultStore,
) -> None:
    """A rebuilt store keeps every result and the next call number."""
    rebuilt = results.ResultStore.from_list(store.to_list())

    assert (len(rebuilt), rebuilt.next_call_number()) == (4, 5)
