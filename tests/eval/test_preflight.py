"""Paid-run admission and accounting, exercised with no provider dispatch."""

import dataclasses
import shutil
import subprocess
from decimal import Decimal
from typing import cast

import pytest

from incidents_agent.agent import state
from incidents_agent.eval import retry
from incidents_agent.eval import runner
from incidents_agent.eval import service
from incidents_agent.eval import single_call
from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts


@pytest.fixture
def paid_options(evaluation_options, monkeypatch, tmp_path):
    """Use a private spend ledger and test credentials."""
    shutil.copytree(service.ROOT / 'config', tmp_path / 'config')
    target = tmp_path / 'splits/re2-ob.json'
    runner.atomic_json(target, runner.read_object(evaluation_options.split))
    # The reference hash must match the locked split at its new path.
    monkeypatch.setattr(service, 'ROOT', tmp_path)
    monkeypatch.setattr(service, 'shared_root', lambda: tmp_path)
    monkeypatch.setattr(service, 'source_fingerprint', lambda _: 'source')
    monkeypatch.setattr(
        config, 'environment', lambda _: {'OPENAI_API_KEY': 'test-only'}
    )

    def git(command, **kwargs):
        if 'branch' in command:
            return 'main'
        if 'status' in command:
            return ''
        return 'test-commit'

    monkeypatch.setattr(subprocess, 'check_output', git)
    return dataclasses.replace(
        evaluation_options,
        dry_run=False,
        model='gpt-small',
        split=target,
        config=tmp_path / 'config/agent.yaml',
        output_root=tmp_path / 'runs/phase-06',
    )


def test_paid_preflight_and_configuration_scoped_spend(paid_options):
    """Charge the current invocation for its own calls only."""
    prepared = service.prepare(dataclasses.replace(paid_options, final=True))
    spend = prepared.spend
    assert spend is not None

    def execute(case, attempts, run_id):
        del attempts
        identity = contracts.Run(run_id, 'final')
        charge = spend.reserve(
            identity, case.case_id, 'openai/gpt-4o-mini', Decimal('0.02')
        )
        spend.finish(charge, 'success', cost=Decimal('0.01'))
        return {'ranks': []}

    other = spend.reserve(
        contracts.Run('unrelated'),
        'other',
        'openai/gpt-4o-mini',
        Decimal('0.03'),
    )
    spend.finish(other, 'success', cost=Decimal('0.03'))
    prepared = dataclasses.replace(prepared, executor=execute)
    first = service.execute(prepared)
    cached = service.execute(prepared)

    assert first.invocation_cost_usd == pytest.approx(0.03)
    assert cached.invocation_cost_usd == 0
    assert prepared.manifest['dry_run'] is False


@pytest.mark.parametrize(
    'change,error',
    [
        ({'workers': 0}, 'workers'),
        ({'agent': 'invalid'}, 'Unknown agent'),
        ({'repeat_id': ''}, 'repeat_id'),
    ],
)
def test_invalid_options_rejected(paid_options, change, error):
    """Invalid run options fail without dispatching a request."""
    with pytest.raises(ValueError, match=error):
        service.prepare(dataclasses.replace(paid_options, **change))


def test_paid_run_requires_shared_cache(paid_options, tmp_path):
    """A custom cache cannot bypass shared paid-result claims."""
    with pytest.raises(ValueError, match='shared evaluation cache'):
        service.prepare(
            dataclasses.replace(paid_options, output_root=tmp_path / 'other')
        )


def test_paid_run_requires_locked_split(paid_options, tmp_path):
    """An alternate split is admitted only for offline fixtures."""
    with pytest.raises(ValueError, match='locked split'):
        service.prepare(
            dataclasses.replace(paid_options, split=tmp_path / 'split.json')
        )


@pytest.mark.parametrize(
    'branch,status,match',
    [
        ('feature', '', 'merged main'),
        ('main', 'modified', 'clean tracked'),
    ],
)
def test_paid_checkout_gate(paid_options, monkeypatch, branch, status, match):
    """Paid runs require a clean main checkout."""
    monkeypatch.setattr(
        subprocess,
        'check_output',
        lambda command, **kwargs: branch if 'branch' in command else status,
    )
    with pytest.raises(ValueError, match=match):
        service.prepare(paid_options)


def test_unusable_model_rejected(paid_options, monkeypatch):
    """Missing credentials fail before a ledger reservation."""
    monkeypatch.setattr(config, 'environment', lambda _: {})
    with pytest.raises(ValueError, match='missing OPENAI'):
        service.prepare(paid_options)


def test_output_cap_checked_before_call(paid_options):
    """Provider output limits are validated while constructing the run."""
    paid_options.config.write_text('max_output_tokens: 100000000\n')
    with pytest.raises(ValueError, match='ceiling'):
        service.prepare(paid_options)


def test_outputs_cannot_enter_task_view(evaluation_options):
    """Scoring artifacts cannot contaminate blind inputs for later runs."""
    with pytest.raises(ValueError, match='outside the blind task view'):
        service.prepare(
            dataclasses.replace(
                evaluation_options,
                output_root=evaluation_options.tasks_root / 'outputs',
            )
        )


def test_memory_requires_an_explicit_frozen_snapshot(paid_options, tmp_path):
    """Memory configuration cannot silently open or create a mutable store."""
    path = tmp_path / 'config/agent.yaml'
    path.write_text(
        path.read_text().replace('memory: false', 'memory: true', 1)
    )

    with pytest.raises(ValueError, match='requires --memory-path'):
        service.prepare(paid_options)


def test_paid_client_retries_one_request_in_place(paid_options, tmp_path):
    """A transient error late in a run must not repay the earlier turns."""
    prepared = service.prepare(paid_options)

    executor = cast(single_call.Executor, prepared.executor)
    guarded = cast(
        client.GuardedClient,
        executor.client_factory(state.Task(folder=tmp_path)),
    )

    assert guarded._policy.attempts == retry.REQUEST_ATTEMPTS
    assert guarded._policy.retry_window == retry.WINDOW_S
