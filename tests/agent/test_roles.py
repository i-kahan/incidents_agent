"""Briefs, verdicts and challenges: schemas, validation and merging."""

import functools
from concurrent import futures
from pathlib import Path

import pytest

from incidents_agent import ingest
from incidents_agent.agent import investigate
from incidents_agent.agent import report
from incidents_agent.agent import roles
from incidents_agent.llm import fake
from tests.agent import scripted

_LOCK_QUOTE = 'inventory-db Lock wait timeout'


def _session(
    ingested: ingest.IngestResult, tmp_path: Path
) -> investigate.Session:
    session = scripted.make_session(ingested, fake.ScriptedClient([]), tmp_path)
    assert session.ledger is not None
    session.ledger.apply(
        {
            'operations': [
                {
                    'op': 'add',
                    'statement': 'lock contention',
                    'component': 'inventory-db',
                },
                {
                    'op': 'add',
                    'statement': 'queue saturation',
                    'component': 'api-gateway',
                },
            ]
        }
    )
    return session


def _call_as(
    session: investigate.Session, role: str, hypothesis_id: str | None
) -> str:
    """Make a real lock search and record it as `role`'s call."""
    stored = session.executor.call('search_text', scripted.LOCK_SEARCH)
    session.record.record(
        'tool_call',
        role=role,
        hypothesis_id=hypothesis_id,
        **stored.to_dict(),
    )
    return stored.call_id


def _cite(call_id: str) -> list[dict[str, object]]:
    return [{'call_id': call_id, 'locator': 'service', 'quote': _LOCK_QUOTE}]


def _verdict(hid: str, kind: roles.VerdictKind) -> roles.Verdict:
    return roles.Verdict(hid, kind)


def test_merge_verdicts_keeps_one_verdict_per_hypothesis() -> None:
    """The reducer combines verdicts keyed by hypothesis id."""
    merged = roles.merge_verdicts(
        {'H1': _verdict('H1', 'supported')}, {'H2': _verdict('H2', 'rejected')}
    )

    assert sorted(merged) == ['H1', 'H2']


def test_merge_verdicts_refuses_to_overwrite_a_different_verdict() -> None:
    """No verdict can overwrite another for the same hypothesis."""
    with pytest.raises(ValueError, match='H1'):
        roles.merge_verdicts(
            {'H1': _verdict('H1', 'supported')},
            {'H1': _verdict('H1', 'rejected')},
        )


def test_merge_verdicts_from_concurrent_testers_keeps_them_all() -> None:
    """Reducing verdicts produced on threads loses none of them."""
    ids = [f'H{n}' for n in range(1, 9)]
    with futures.ThreadPoolExecutor(8) as pool:
        produced = list(
            pool.map(lambda hid: {hid: _verdict(hid, 'undecided')}, ids)
        )

    empty: dict[str, roles.Verdict] = {}
    merged = functools.reduce(roles.merge_verdicts, produced, empty)

    assert sorted(merged, key=roles.id_order) == ids


def test_id_order_puts_h2_before_h10() -> None:
    """Hypothesis ids sort by number, not by text."""
    assert sorted(['H10', 'H2', 'H1'], key=roles.id_order) == [
        'H1',
        'H2',
        'H10',
    ]


@pytest.mark.parametrize(
    ('args', 'error'),
    [
        pytest.param(
            {'outcome': 'dispatch', 'briefs': []}, '1 to 3', id='no_briefs'
        ),
        pytest.param(
            {
                'outcome': 'dispatch',
                'briefs': [
                    {'hypothesis_id': 'H9', 'confirm_if': 'a', 'refute_if': 'b'}
                ],
            },
            'unknown hypothesis',
            id='unknown_id',
        ),
        pytest.param(
            {
                'outcome': 'dispatch',
                'briefs': [
                    {
                        'hypothesis_id': 'H1',
                        'confirm_if': 'a',
                        'refute_if': 'b',
                    },
                    {
                        'hypothesis_id': 'H1',
                        'confirm_if': 'a',
                        'refute_if': 'b',
                    },
                ],
            },
            'briefed twice',
            id='duplicate',
        ),
        pytest.param(
            {
                'outcome': 'dispatch',
                'briefs': [{'hypothesis_id': 'H1', 'confirm_if': 'a'}],
            },
            'confirm_if and refute_if',
            id='no_refute',
        ),
        pytest.param({'outcome': 'maybe'}, 'outcome', id='bad_outcome'),
    ],
)
def test_dispatch_refuses_invalid_briefs(
    field_ingest: ingest.IngestResult,
    tmp_path: Path,
    args: dict[str, object],
    error: str,
) -> None:
    """Briefs must name live ledger hypotheses, once each, both ways."""
    session = _session(field_ingest, tmp_path)

    accepted, errors = roles.dispatch_tool(session, 3).validate(args)

    assert accepted is None
    assert error in ' '.join(errors)


def test_dispatch_accepts_briefs_and_returns_them_in_order(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Valid briefs come back as `TesterBrief`s."""
    session = _session(field_ingest, tmp_path)

    accepted, _ = roles.dispatch_tool(session, 3).validate(
        {
            'outcome': 'dispatch',
            'briefs': [
                {'hypothesis_id': 'H2', 'confirm_if': 'a', 'refute_if': 'b'},
                {'hypothesis_id': 'H1', 'confirm_if': 'c', 'refute_if': 'd'},
            ],
        }
    )

    assert accepted is not None
    assert [b.hypothesis_id for b in roles.briefs_from(accepted)] == [
        'H2',
        'H1',
    ]


def test_verdict_for_another_hypothesis_is_refused(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A tester may rule only on its own hypothesis."""
    session = _session(field_ingest, tmp_path)

    accepted, errors = roles.verdict_tool(session, 'H1').validate(
        {'hypothesis_id': 'H2', 'verdict': 'undecided'}
    )

    assert accepted is None
    assert errors == ["hypothesis_id must be 'H1'"]


def test_supported_verdict_citing_another_roles_call_is_refused(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Support must come from a call this tester made itself."""
    session = _session(field_ingest, tmp_path)
    lead_call = _call_as(session, 'lead', None)

    accepted, errors = roles.verdict_tool(session, 'H1').validate(
        {
            'hypothesis_id': 'H1',
            'verdict': 'supported',
            'evidence': _cite(lead_call),
        }
    )

    assert accepted is None
    assert 'must cite a tool call you made' in errors[-1]


def test_supported_verdict_citing_own_call_is_accepted(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A tester's own grounded call supports its verdict."""
    session = _session(field_ingest, tmp_path)
    own = _call_as(session, 'tester', 'H1')

    accepted, _ = roles.verdict_tool(session, 'H1').validate(
        {
            'hypothesis_id': 'H1',
            'verdict': 'supported',
            'confidence': 'high',
            'evidence': _cite(own),
        }
    )

    assert accepted is not None
    found = accepted['verdict_record']
    assert isinstance(found, roles.Verdict)
    assert (found.verdict, found.confidence) == ('supported', 'high')
    assert found.evidence[0].call_id == own


def test_citation_from_outside_the_run_is_refused(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A call id this run never made grounds nothing."""
    session = _session(field_ingest, tmp_path)
    _call_as(session, 'tester', 'H1')

    accepted, errors = roles.verdict_tool(session, 'H1').validate(
        {
            'hypothesis_id': 'H1',
            'verdict': 'supported',
            'evidence': _cite('call_0999'),
        }
    )

    assert accepted is None
    assert any('evidence rejected' in error for error in errors)


@pytest.mark.parametrize(
    ('outcome', 'own', 'expected'),
    [
        pytest.param('holds', True, 'supported', id='holds_with_evidence'),
        pytest.param('holds', False, 'inconclusive', id='holds_no_evidence'),
        pytest.param('disproved', True, 'refuted', id='disproved'),
    ],
)
def test_challenge_maps_onto_ledger_verification(
    field_ingest: ingest.IngestResult,
    tmp_path: Path,
    outcome: str,
    own: bool,
    expected: str,
) -> None:
    """Skeptic outcomes become verification outcomes on the ledger."""
    session = _session(field_ingest, tmp_path)
    call_id = _call_as(session, 'skeptic', None)

    accepted, _ = roles.challenge_tool(session, 'H1').validate(
        {
            'hypothesis_id': 'H1',
            'check': 'upstream?',
            'outcome': outcome,
            'evidence': _cite(call_id) if own else [],
        }
    )

    assert session.ledger is not None
    assert accepted is not None
    assert accepted['verification'] == expected
    assert session.ledger.entries['H1'].verification == expected


def test_disproval_without_own_evidence_is_refused(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """The skeptic cannot disprove a claim without having checked."""
    session = _session(field_ingest, tmp_path)

    accepted, errors = roles.challenge_tool(session, 'H1').validate(
        {'hypothesis_id': 'H1', 'check': 'x', 'outcome': 'disproved'}
    )

    assert accepted is None
    assert 'must cite a tool call you made' in errors[-1]


def test_skeptic_opening_holds_claim_and_cited_results_only(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """The skeptic's input is the statement, citations and their results."""
    session = _session(field_ingest, tmp_path)
    assert session.ledger is not None
    call_id = _call_as(session, 'lead', None)
    session.ledger.apply(
        {
            'operations': [
                {'op': 'revise', 'id': 'H1', 'evidence': _cite(call_id)}
            ]
        }
    )

    opening = roles.skeptic_opening(
        session.ledger.entries['H1'],
        session.executor.store,
        tool_calls=2,
        direction='',
    )

    assert opening.startswith('Claim H1: inventory-db is the root')
    assert f'- {call_id}, service: "{_LOCK_QUOTE}"' in opening
    assert 'tx-200' in opening
    assert 'api-gateway' not in opening.split('Evidence')[0]


def test_role_tools_withhold_ledger_and_memory_from_testers(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Testers get data tools and read_result; the lead gets everything."""
    session = _session(field_ingest, tmp_path)

    tester = roles.role_tools(session, 'tester')

    assert roles.role_tools(session, 'lead') is None
    assert tester is not None
    assert {'dependency_graph', 'read_result'} <= tester
    assert 'update_hypotheses' not in tester


@pytest.mark.parametrize(
    ('verdicts', 'order'),
    [
        pytest.param(
            {'H1': 'rejected', 'H2': 'supported'}, ['H2', 'H1'], id='sup_rej'
        ),
        pytest.param(
            {'H1': 'rejected', 'H2': 'undecided'}, ['H2', 'H1'], id='und_rej'
        ),
    ],
)
def test_undecided_ranks_between_supported_and_rejected(
    field_ingest: ingest.IngestResult,
    tmp_path: Path,
    verdicts: dict[str, roles.VerdictKind],
    order: list[str],
) -> None:
    """Merged verdicts rank supported, then undecided, then rejected."""
    session = _session(field_ingest, tmp_path)
    book = session.ledger
    assert book is not None
    own = _call_as(session, 'tester', 'H2')
    evidence = (
        [report.EvidenceRef(own, 'service', _LOCK_QUOTE)]
        if verdicts['H2'] == 'supported'
        else []
    )

    book.apply_verdict(
        'H1', verdicts['H1'], confidence='low', evidence=[], reason='no'
    )
    book.apply_verdict(
        'H2', verdicts['H2'], confidence='low', evidence=evidence, reason='?'
    )

    assert [entry.id for entry in book.ranked()] == order


def test_apply_verdict_leaves_a_refuted_hypothesis_alone(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A verdict cannot revive a hypothesis the skeptic refuted."""
    session = _session(field_ingest, tmp_path)
    book = session.ledger
    assert book is not None
    book.entries['H1'].verification = 'refuted'

    errors = book.apply_verdict(
        'H1', 'supported', confidence='high', evidence=[], reason=None
    )

    assert errors == ['H1 was refuted and stays rejected']
