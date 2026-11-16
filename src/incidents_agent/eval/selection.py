"""Resolve case selections before applying the held-out gate."""

from collections.abc import Sequence

from incidents_agent.eval import split


def resolve(
    selector: str,
    partition: split.Split,
    *,
    final: bool = False,
    limit: int | None = None,
    field_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return stable selected IDs; any actual held-out case requires final."""
    if limit is not None and limit < 1:
        raise ValueError('limit must be positive')
    if selector == 'field':
        selected = sorted(field_ids)
        if not selected:
            raise ValueError('No field cases configured')
    else:
        known = set(partition.tuning + partition.heldout)
        choices = {
            'tuning': partition.tuning,
            'heldout': partition.heldout,
            'all': tuple(known),
        }
        requested = choices.get(selector, tuple(selector.split(',')))
        if not requested or len(set(requested)) != len(requested):
            raise ValueError('Selection must be nonempty and unique')
        if set(requested) - known:
            raise ValueError('Unknown benchmark case ID')
        selected = sorted(requested)
    selected = selected[:limit]
    if not final and set(selected) & set(partition.heldout):
        raise ValueError('Held-out cases require --final')
    return tuple(selected)
