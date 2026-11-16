"""List configured models and optionally send guarded live probes."""

import argparse
import json
import logging
import uuid
from dataclasses import asdict
from pathlib import Path

from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import litellm_transport
from incidents_agent.llm import pricing
from scripts import budget

MODELS_FILE = Path('config/models.yaml')


def probe_request(alias: str) -> contracts.ChatRequest:
    """Build a small text-only request for provider wiring checks."""
    return contracts.ChatRequest(
        alias,
        'Keep the response short.',
        16,
        history=(contracts.Message('user', 'Reply with OK.'),),
    )


def main() -> int:
    """Print model inventory and run live probes only with --live."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--budget-usd')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format='%(levelname)s: %(message)s'
    )
    try:
        env = config.environment(Path('.env'))
        models = config.load_models(MODELS_FILE)
        catalog = pricing.bundled_catalog()
        config.load_budget(
            budget.BUDGET_FILE, override=args.budget_usd, environ=env
        )
        inventory = [
            client.model_info(models, env, catalog, alias)
            for alias in models.aliases
        ]
    except (config.ConfigError, OSError) as exc:
        parser.error(str(exc))

    print(f'Default: {models.default}')
    for info in inventory:
        rates = info.price.tiers[0][1] if info.price else None
        costs = (
            'unknown'
            if rates is None
            else ', '.join(
                f'{field}=${rate * 1_000_000:f}'
                for field, rate in zip(rates._fields, rates, strict=True)
            )
        )
        state = 'usable' if info.usable else '; '.join(info.issues)
        pin = (
            'placeholder'
            if info.spec.placeholder
            else 'pinned'
            if info.spec.pinned
            else 'floating'
        )
        tiers = ' (tiered)' if info.price and len(info.price.tiers) > 1 else ''
        print(
            f'{info.spec.alias} ({info.spec.id}): {state}; {pin}; '
            f'price per million tokens: {costs}{tiers}'
        )

    if not args.live:
        return 0
    aliases = [info.spec.alias for info in inventory if info.usable]
    if not aliases:
        print('No usable model to probe.')
        return 1

    spend = budget.open_ledger(args.budget_usd, env)
    llm = client.GuardedClient(
        models,
        env,
        spend,
        litellm_transport.LiteLLMTransport(),
        catalog=catalog,
        policy=client.RetryPolicy(attempts=1),
    )
    run = contracts.Run(f'check-models-{uuid.uuid4().hex}')
    folder = spend.path.parent / run.id
    folder.mkdir()
    manifest: dict[str, object] = {
        'run_id': run.id,
        'aliases': aliases,
        'limit_usd': str(spend.settings().limit),
    }
    failures: dict[str, str] = {}
    try:
        for alias in aliases:
            try:
                result = llm.complete(probe_request(alias), run=run)
                print(
                    f'{alias}: provider served {result.response.served_model}'
                )
            except (
                contracts.ModelUnavailable,
                contracts.BudgetRefused,
                contracts.ProviderError,
                contracts.RetryDeferred,
                contracts.BillingUncertain,
                config.ConfigError,
            ) as exc:
                failures[alias] = str(exc)
                print(f'{alias}: {exc}')
    finally:
        manifest['failures'] = failures
        manifest['counters'] = asdict(spend.counters(run))
        (folder / 'manifest.json').write_text(
            json.dumps(manifest, default=str, indent=2) + '\n', encoding='utf-8'
        )
        with (folder / 'trajectory.jsonl').open(
            'w', encoding='utf-8'
        ) as output:
            for record in spend.attempt_records(run):
                output.write(json.dumps(record) + '\n')
        print(f'Probe output: {folder}')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
