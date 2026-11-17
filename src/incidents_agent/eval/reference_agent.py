"""Run RCAEval's reference loop with the guarded model client."""

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from typing import cast

from incidents_agent.agent import config
from incidents_agent.agent import state
from incidents_agent.eval import agent_adapter
from incidents_agent.eval import runner
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger

POLICY = {'output_tokens': 1024, 'temperature': 0, 'prompt_version': '2'}


class Executor:
    """Adapt upstream's injectable completion to the guarded project client."""

    def __init__(
        self,
        client_factory: Callable[[state.Task], contracts.ModelClient],
        settings: config.AgentConfig,
        spend: ledger.SpendLedger | None,
    ) -> None:
        """Store the guarded client factory and selected case limits."""
        self.client_factory = client_factory
        self.settings = settings
        self.spend = spend

    def __call__(
        self, case: runner.Case, attempts: Path, run_id: str
    ) -> dict[str, object]:
        """Run one upstream case and save its observable tool trajectory."""
        # RCAEval has no type stubs and is only needed for this arm.
        from RCAEval.agent import react  # type: ignore[import-untyped]

        task = agent_adapter.task_for(case)
        client = self.client_factory(task)
        identity = contracts.Run(run_id)
        folder = attempts / run_id
        folder.mkdir(parents=True, exist_ok=True)
        requested: set[str] = set()
        served: set[str] = set()

        def completion(
            *,
            model: str,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
            temperature: float,
            tool_choice: dict[str, dict[str, str]] | None = None,
        ) -> SimpleNamespace:
            del model, temperature
            names = None if tool_choice is None else tool_choice['function']
            definitions = [
                cast(dict[str, object], spec['function']) for spec in tools
            ]
            specs = tuple(
                contracts.Tool(
                    str(item['name']),
                    str(item.get('description', '')),
                    cast(dict[str, contracts.JSON], item['parameters']),
                )
                for item in definitions
                if names is None or item['name'] == names['name']
            )
            history = []
            for message in messages[1:]:
                calls = cast(
                    list[dict[str, object]], message.get('tool_calls', [])
                )
                turns = []
                for call in calls:
                    function = cast(dict[str, str], call['function'])
                    turns.append(
                        contracts.ToolCall(
                            str(call['id']),
                            function['name'],
                            function['arguments'],
                        )
                    )
                history.append(
                    contracts.Message(
                        cast(
                            Literal['user', 'assistant', 'tool'],
                            message['role'],
                        ),
                        str(message['content']),
                        tuple(turns),
                        cast(str | None, message.get('tool_call_id')),
                    )
                )
            result = client.complete(
                contracts.ChatRequest(
                    self.settings.model_alias,
                    str(messages[0]['content']),
                    self.settings.max_output_tokens,
                    specs,
                    history=tuple(history),
                ),
                run=identity,
                case_id=case.case_id,
            )
            requested.add(result.requested_model)
            actual = result.response
            served.add(actual.served_model)
            calls_out = [
                SimpleNamespace(
                    id=call.id,
                    function=SimpleNamespace(
                        name=call.name, arguments=call.arguments
                    ),
                )
                for call in actual.message.tool_calls
            ]
            reply = SimpleNamespace(
                content=actual.message.text, tool_calls=calls_out
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=reply)],
                usage=SimpleNamespace(
                    prompt_tokens=actual.usage.input_tokens,
                    completion_tokens=actual.usage.output_tokens,
                ),
                cost_usd=float(result.cost_usd),
            )

        try:
            prediction, records = react.run_case(
                case.folder,
                self.settings.model_alias,
                max_steps=self.settings.limits.model_turns - 1,
                max_cost_usd=float(self.settings.limits.cost_usd),
                completion=completion,
                cost=lambda response: response.cost_usd,
                prompt_version=POLICY['prompt_version'],
            )
        finally:
            if self.spend is not None:
                counters = self.spend.counters(identity)
                runner.atomic_json(
                    folder / 'usage.json',
                    {
                        **dataclasses.asdict(counters.usage),
                        'cost_usd': float(counters.spent),
                        'held_usd': float(counters.held),
                    },
                )
        path = folder / 'rcaeval-trajectory.jsonl'
        path.write_text(
            ''.join(json.dumps(row, default=str) + '\n' for row in records)
        )
        usage = prediction['usage']
        if self.spend is not None:
            usage.update(runner.read_object(folder / 'usage.json'))
        usage['model_turns'] = usage['steps']
        usage['outcome'] = prediction['stop_reason']
        runner.atomic_json(folder / 'usage.json', usage)
        output: dict[str, object] = {
            **prediction,
            'trajectory': str(path.resolve()),
            'requested_models': sorted(requested),
            'served_models': sorted(served),
        }
        return output
