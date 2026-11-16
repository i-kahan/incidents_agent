"""Tests for tool registry call numbering."""

from incidents_agent.tools import registry
from incidents_agent.tools import schema


def _echo_registry(first: int) -> registry.ToolRegistry:
    tools = registry.ToolRegistry(first_call_number=first)
    tools.register(
        schema.ToolSpec(
            'echo', 'Echo.', {'type': 'object'}, lambda a: {'ok': True}
        )
    )
    return tools


def test_registry_numbers_calls_from_first_call_number() -> None:
    """A registry for a continued run never reuses an earlier call id."""
    tools = _echo_registry(7)

    result = tools.call('echo', {})

    assert result['call_id'] == 'call_0007'


def test_registry_numbers_calls_from_one_by_default() -> None:
    """Without a starting number, ids start at call_0001."""
    tools = _echo_registry(1)
    tools.call('echo', {})

    assert tools.get_call_result_id() == 'call_0002'
