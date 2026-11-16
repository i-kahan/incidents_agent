"""Shared typed assertion helpers for Phase 03 tool tests."""


def as_dict(value: object) -> dict[str, object]:
    """Assert that a JSON-like value is a dict with string keys."""
    assert isinstance(value, dict)
    return {str(k): v for k, v in value.items()}


def as_dict_list(value: object) -> list[dict[str, object]]:
    """Assert that a JSON-like value is a list of dicts."""
    assert isinstance(value, list)
    return [as_dict(item) for item in value]


def untrusted_content(response: dict[str, object]) -> dict[str, object]:
    """Extract and type-check `response['untrusted_data']['content']`."""
    envelope = as_dict(response['untrusted_data'])
    assert envelope['_untrusted_source'] is True
    return as_dict(envelope['content'])
