"""Tests for the investigate loop, the context policy and finalisation."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from incidents_agent import ingest
from incidents_agent.agent import config
from incidents_agent.agent import executor
from incidents_agent.agent import finalise
from incidents_agent.agent import investigate
from incidents_agent.agent import state
from incidents_agent.llm import contracts
from incidents_agent.llm import fake
from tests.agent import scripted

_NO_VERIFY = config.AgentConfig(verify=False)
_PLAIN = config.AgentConfig(ledger=False, verify=False)


def _run(
    ingested: ingest.IngestResult,
    tmp_path: Path,
    turns: list[contracts.Message | contracts.ChatResult | Exception],
    *,
    agent_config: config.AgentConfig = _NO_VERIFY,
    task: state.Task | None = None,
    brief: investigate.Brief | None = None,
) -> tuple[investigate.Session, investigate.LoopResult, fake.ScriptedClient]:
    client = fake.ScriptedClient(turns)
    session = scripted.make_session(
        ingested, client, tmp_path, agent_config=agent_config, task=task
    )
    result = investigate.investigate(
        session,
        brief or investigate.Brief(),
        scripted.conversation(session),
        investigate.submit_tool(session),
    )
    return session, result, client


def _single(session: investigate.Session) -> state.Budget:
    budget = session.budget
    assert isinstance(budget, state.Budget)
    return budget


def _tool_replies(client: fake.ScriptedClient) -> list[dict[str, object]]:
    history = client.requests[-1][0].history
    return [
        json.loads(str(message.text))
        for message in history
        if message.role == 'tool'
    ]


def test_investigate_happy_path_submits_cited_ranking(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Search, cite, submit: the report ranks the cited component."""
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.submit('inventory-db', confidence='medium'),
        ],
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert (final.outcome, final.ranks, final.stop_reason) == (
        'provisional',
        ('inventory-db',),
        'submitted',
    )
    assert final.causes[0].evidence[0].call_id == 'call_0001'


def test_investigate_at_tool_call_limit_forces_finalisation(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """After the limit, one terminal-only turn, then a forced stop."""
    limits = config.Limits(tool_calls=2)
    session, result, client = _run(
        field_ingest,
        tmp_path,
        [
            scripted.calls(
                ('list_tables', {}), ('search_text', scripted.LOCK_SEARCH)
            ),
            scripted.call('find_onset'),
        ],
        agent_config=config.AgentConfig(verify=False, limits=limits),
    )

    final = finalise.finalise(session, submission=None, stop_reason='x')

    assert (result.stop_reason, _single(session).tool_calls) == (
        'tool_call_limit',
        2,
    )
    assert final.outcome == 'inconclusive'
    assert len(client.requests) == 2


def test_investigate_calls_past_allowance_get_limit_error(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Calls in one turn beyond the remaining share are answered with errors."""
    limits = config.Limits(tool_calls=3, verify_tool_calls=2)
    _, _, client = _run(
        field_ingest,
        tmp_path,
        [
            scripted.calls(('list_tables', {}), ('find_onset', {})),
            scripted.submit(outcome='no_fault_found'),
        ],
        agent_config=config.AgentConfig(limits=limits),
    )

    replies = _tool_replies(client)

    assert replies[0]['ok'] is True
    assert replies[1]['error'] == {
        'code': 'tool_call_limit',
        'message': 'The tool-call limit for this step is reached; call submit.',
    }


def test_investigate_stops_at_cost_cap(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A turn that settles past the cost share stops the loop."""
    _, result, client = _run(
        field_ingest,
        tmp_path,
        [scripted.costly(scripted.call('list_tables'), '0.06')],
    )

    assert (result.stop_reason, len(client.requests)) == ('cost_cap', 1)


def test_investigate_rejects_submit_outside_candidates(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """An invalid rank is refused and the model may submit again."""
    task = state.Task(
        scripted.FIELD_CASE, candidate_services=('inventory-db', 'api-gateway')
    )
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [scripted.submit('database'), scripted.submit('Inventory-DB')],
        task=task,
    )

    rejected = session.record.of_type('submit_rejected')

    assert 'not in the candidate list' in str(rejected[0]['errors'])
    assert result.accepted is not None
    assert (result.invalid_calls, result.accepted['ranks']) == (
        1,
        ['inventory-db'],
    )


def test_investigate_surfaces_tool_error_and_continues(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A failing tool returns an error result and the run goes on."""
    _, result, client = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('run_sql', query='DELETE FROM metrics'),
            scripted.submit(outcome='no_fault_found'),
        ],
    )

    assert _tool_replies(client)[0]['ok'] is False
    assert result.stop_reason == 'submitted'


@pytest.mark.parametrize(
    ('failure', 'reason'),
    [
        pytest.param(contracts.BudgetRefused('x'), 'spend_refused', id='spend'),
        pytest.param(
            contracts.ProviderError('x'), 'model_error', id='provider'
        ),
    ],
)
def test_investigate_model_failure_ends_step(
    field_ingest: ingest.IngestResult,
    tmp_path: Path,
    failure: Exception,
    reason: str,
) -> None:
    """Spend refusals and provider errors end the step with a stop reason."""
    session, result, _ = _run(field_ingest, tmp_path, [failure])

    assert result.stop_reason == reason
    assert session.record.of_type('model_error')[0]['reason'] == reason


def test_trimmed_result_is_digested_but_still_citable(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """An older result becomes a digest yet its call id stays citable."""
    policy = config.ContextPolicy(keep_recent_results=1, digest_block=1)
    session, result, client = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.call('list_tables'),
            scripted.call('read_result', call_id='call_0001'),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.submit('inventory-db'),
        ],
        agent_config=config.AgentConfig(verify=False, context=policy),
    )

    sent = client.requests[2][0].history

    assert 'Digest' in str(sent[1].text)
    assert 'inventory-db Lock wait timeout' in str(
        client.requests[3][0].history[-1].text
    )
    assert session.ledger is not None
    assert len(session.ledger.entries['H1'].evidence_for) == 1
    assert result.stop_reason == 'submitted'


def test_digest_keeps_call_id_and_locator(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A digest names its call, tool and locator and how to re-read it."""
    session = scripted.make_session(
        field_ingest, fake.ScriptedClient([]), tmp_path
    )
    stored = session.executor.call('search_text', dict(scripted.LOCK_SEARCH))

    text = executor.digest(stored, config.ContextPolicy())

    header = json.loads(text.splitlines()[0])
    assert (header['digest_of'], header['locator']['table']) == (
        'call_0001',
        'service',
    )
    assert 'read_result call_id="call_0001"' in text


def test_finalise_rank_without_citation_is_uncited_and_inconclusive(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A name ranked with no hypothesis stays in the ranking, marked."""
    session, result, _ = _run(
        field_ingest, tmp_path, [scripted.submit('inventory-db')]
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    cause = final.causes[0]
    assert (final.outcome, final.ranks) == ('inconclusive', ('inventory-db',))
    assert (cause.cited, cause.confidence, cause.evidence) == (False, 'low', ())
    assert 'uncited' in cause.confidence_reasons[0]


def test_finalise_keeps_uncited_ranks_after_the_cited_ones(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Uncited names follow cited causes and are no target for actions."""
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.submit(
                'order-service',
                'inventory-db',
                'api-gateway',
                actions=[
                    {
                        'hypothesis_id': 'order-service',
                        'action': 'restart it',
                        'risk': 'disruptive',
                        'how_to_verify': 'latency recovers',
                    }
                ],
            ),
        ],
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert (final.outcome, final.ranks) == (
        'provisional',
        ('inventory-db', 'order-service', 'api-gateway'),
    )
    assert [cause.cited for cause in final.causes] == [True, False, False]
    assert [cause.rank for cause in final.causes] == [1, 2, 3]
    assert final.actions == ()
    assert "action 0: 'order-service' is not a ranked hypothesis" in (
        final.dropped
    )


def test_finalise_drops_rank_whose_hypothesis_was_rejected(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A name the ledger rejected is not brought back as an uncited rank."""
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.call(
                'update_hypotheses',
                operations=[{'op': 'reject', 'id': 'H1', 'reason': 'no'}],
            ),
            scripted.submit('inventory-db'),
        ],
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert (final.outcome, final.ranks) == ('inconclusive', ())
    assert final.dropped == ("rank 'inventory-db': H1 was rejected",)


def test_ledger_forced_end_keeps_last_stated_ranking_as_uncited(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A forced end with the ledger on still reports the stated candidates."""
    task = state.Task(scripted.FIELD_CASE, candidate_services=('a', 'b'))
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [scripted.submit('B', 'zzz')],
        agent_config=config.AgentConfig(
            verify=False, limits=config.Limits(model_turns=1)
        ),
        task=task,
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert (result.stop_reason, final.outcome, final.ranks) == (
        'model_turn_limit',
        'inconclusive',
        ('b',),
    )
    assert 'forced finalisation: model_turn_limit' in (
        final.causes[0].confidence_reasons
    )


def test_finalise_no_fault_found_is_its_own_outcome(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Submitting no_fault_found finalises as no_fault_found."""
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [scripted.submit(outcome='no_fault_found')],
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert (final.outcome, final.ranks, final.data_requests) == (
        'no_fault_found',
        (),
        (),
    )


def test_no_fault_found_refused_while_a_hypothesis_is_supported(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A supported hypothesis must be rejected before no_fault_found."""
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(
                evidence=[scripted.LOCK_EVIDENCE], status='supported'
            ),
            scripted.submit(outcome='no_fault_found'),
            scripted.submit('inventory-db'),
        ],
    )

    assert 'supported hypothesis exists' in str(
        session.record.of_type('submit_rejected')[0]['errors']
    )
    assert result.accepted is not None


def test_finalise_orders_actions_and_drops_invalid_ones(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Read-only checks come first; actions with bad targets are dropped."""
    actions = [
        {
            'hypothesis_id': 'H1',
            'action': 'restart inventory-db',
            'risk': 'disruptive',
            'how_to_verify': 'lock waits drop',
        },
        {
            'hypothesis_id': 'H1',
            'action': 'list long transactions',
            'risk': 'read_only',
            'how_to_verify': 'a long holder appears',
        },
        {
            'hypothesis_id': 'H7',
            'action': 'x',
            'risk': 'read_only',
            'how_to_verify': 'y',
        },
    ]
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.submit('inventory-db', actions=actions),
        ],
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert [action.risk for action in final.actions] == [
        'read_only',
        'disruptive',
    ]
    assert "action 2: 'H7' is not a ranked hypothesis" in final.dropped


def test_finalise_data_requests_only_for_open_hypotheses(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Requests for rejected hypotheses are dropped; open ones get one."""
    requests = [
        {'hypothesis_id': 'H2', 'what': 'a', 'source': 'b', 'effect': 'c'},
        {
            'hypothesis_id': 'H1',
            'what': 'db locks',
            'source': 'db',
            'effect': 'e',
        },
    ]
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.add_hypothesis(evidence=[scripted.LOCK_EVIDENCE]),
            scripted.add_hypothesis('api-gateway'),
            scripted.call(
                'update_hypotheses',
                operations=[{'op': 'reject', 'id': 'H2', 'reason': 'victim'}],
            ),
            scripted.submit('inventory-db', data_requests=requests),
        ],
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert [item.hypothesis_id for item in final.data_requests] == ['H1']
    assert "data request 0: 'H2' is not an open or undecided hypothesis" in (
        final.dropped
    )


def test_plain_loop_forced_end_uses_last_stated_ranking(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """With the ledger off, a forced end reports the last stated ranking."""
    task = state.Task(scripted.FIELD_CASE, candidate_services=('a', 'b'))
    limits = config.Limits(model_turns=1)
    session, result, _ = _run(
        field_ingest,
        tmp_path,
        [scripted.submit('b', 'zzz')],
        agent_config=config.AgentConfig(
            ledger=False, verify=False, limits=limits
        ),
        task=task,
    )

    final = finalise.finalise(
        session, submission=result.accepted, stop_reason=result.stop_reason
    )

    assert (result.stop_reason, final.ranks, final.outcome) == (
        'model_turn_limit',
        ('b',),
        'provisional',
    )
    assert final.causes[0].confidence == 'low'


def test_plain_loop_tool_list_has_no_ledger_tool(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """The ablated loop offers the same data tools without the ledger."""
    _, _, client = _run(
        field_ingest,
        tmp_path,
        [scripted.submit(outcome='no_fault_found')],
        agent_config=_PLAIN,
    )

    names = [tool.name for tool in client.requests[0][0].tools]

    assert 'update_hypotheses' not in names
    assert {'run_sql', 'read_result', 'submit'} <= set(names)


def test_budget_cost_is_charged_from_settled_results(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Settled call costs accumulate in the budget."""
    session, _, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.costly(scripted.call('list_tables'), '0.001'),
            scripted.costly(scripted.submit(outcome='no_fault_found'), '0.002'),
        ],
    )

    assert _single(session).cost_usd == Decimal('0.003')


def test_brief_tool_allowlist_hides_and_refuses_other_tools(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A role sees only its tools; a call to any other is refused."""
    allowed = frozenset({'search_text', 'read_result'})
    _, _, client = _run(
        field_ingest,
        tmp_path,
        [
            scripted.add_hypothesis(evidence=[]),
            scripted.submit(outcome='no_fault_found'),
        ],
        brief=investigate.Brief(role='tester', tools=allowed),
    )

    names = {tool.name for tool in client.requests[0][0].tools}
    replies = _tool_replies(client)

    assert names == allowed | {'submit'}
    assert replies[0]['error'] == {
        'code': 'unknown_tool',
        'message': "No tool named 'update_hypotheses'",
    }


def test_brief_system_prompt_replaces_the_agent_prompt(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A role's own system prompt is the one sent."""
    _, _, client = _run(
        field_ingest,
        tmp_path,
        [scripted.submit(outcome='no_fault_found')],
        brief=investigate.Brief(role='skeptic', system='Disprove it.'),
    )

    assert client.requests[0][0].system_prompt == 'Disprove it.'


def test_brief_hypothesis_id_tags_every_event(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """Every event of a tester's step names its role and hypothesis."""
    session, _, _ = _run(
        field_ingest,
        tmp_path,
        [
            scripted.call('search_text', **scripted.LOCK_SEARCH),
            scripted.submit(outcome='no_fault_found'),
        ],
        brief=investigate.Brief(role='tester', hypothesis_id='H1'),
    )

    tags = {
        (event['type'], event['role'], event.get('hypothesis_id'))
        for event in session.record.events
    }

    assert tags == {
        ('model_call', 'tester', 'H1'),
        ('tool_call', 'tester', 'H1'),
    }


def test_loop_on_role_allowance_stops_at_the_role_share(
    field_ingest: ingest.IngestResult, tmp_path: Path
) -> None:
    """A role's loop spends its own allowance, not the whole run's."""
    client = fake.ScriptedClient(
        [
            scripted.calls(
                ('list_tables', {}), ('search_text', scripted.LOCK_SEARCH)
            ),
            scripted.call('find_onset'),
        ]
    )
    session = scripted.make_session(
        field_ingest, client, tmp_path, agent_config=_NO_VERIFY
    )
    shared = state.SharedBudget(config.Limits())
    session.budget = shared.allocate('tester', tool_calls=1, model_turns=5)

    result = investigate.investigate(
        session,
        investigate.Brief(role='tester'),
        scripted.conversation(session),
        investigate.submit_tool(session),
    )

    assert result.stop_reason == 'tool_call_limit'
    assert shared.to_dict()['tool_calls'] == 1
