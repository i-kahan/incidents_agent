"""Frozen sampling and human-entered support/action audit judgments."""

import hashlib
import json
import random
from collections.abc import Mapping
from collections.abc import Sequence

ACTION_POINTS = (
    'follows_diagnosis',
    'specific',
    'correct_risk',
    'verification',
)


def digest(report: Mapping[str, object]) -> str:
    """Hash the exact serialized report content used by an audit."""
    return hashlib.sha256(
        json.dumps(report, sort_keys=True).encode()
    ).hexdigest()


def prepare(
    reports: Mapping[str, Mapping[str, object]],
    tuning_ids: Sequence[str],
    *,
    seed: int = 6,
) -> dict[str, object]:
    """Freeze 15 sorted-and-seeded tuning reports; judgments start unset."""
    if set(reports) - set(tuning_ids):
        raise ValueError('Audit accepts tuning reports only')
    if len(reports) < 15:
        raise ValueError('Audit requires at least 15 tuning reports')
    chosen = sorted(random.Random(seed).sample(sorted(reports), 15))
    entries = []
    for case_id in chosen:
        report = reports[case_id]
        claims = []
        causes = report.get('causes', [])
        actions = report.get('actions', [])
        if not isinstance(causes, list) or not isinstance(actions, list):
            raise ValueError('Report causes/actions must be lists')
        for cause in causes:
            if not isinstance(cause, dict):
                raise ValueError('Invalid report cause')
            for evidence in cause.get('evidence', []):
                claims.append(
                    {
                        'statement': cause.get('statement'),
                        'evidence': evidence,
                        'supports_claim': None,
                    }
                )
        entries.append(
            {
                'case_id': case_id,
                'report_sha256': digest(report),
                'claims': claims,
                'actions': [
                    {
                        'action': action,
                        'judgments': dict.fromkeys(ACTION_POINTS),
                    }
                    for action in actions
                ],
            }
        )
    return {'version': 1, 'seed': seed, 'sample_size': 15, 'reports': entries}


def summarize(
    template: Mapping[str, object],
    completed: Mapping[str, object],
) -> dict[str, object]:
    """Accept only judgment edits and summarize completed human reviews."""
    expected = json.loads(json.dumps(template))
    actual = json.loads(json.dumps(completed))
    support: list[bool | None] = []
    action_points: dict[str, list[bool | None]] = {
        key: [] for key in ACTION_POINTS
    }
    try:
        for record in actual['reports']:
            for claim in record['claims']:
                support.append(claim['supports_claim'])
                claim['supports_claim'] = None
            for action in record['actions']:
                for key in ACTION_POINTS:
                    action_points[key].append(action['judgments'][key])
                    action['judgments'][key] = None
    except (KeyError, TypeError) as error:
        raise ValueError('Malformed audit package') from error
    if actual != expected:
        raise ValueError('Frozen sample, hashes, claims or actions changed')

    def rate(values: Sequence[bool | None]) -> dict[str, object]:
        if any(
            value is not None and type(value) is not bool for value in values
        ):
            raise ValueError('Audit judgments must be true, false or null')
        judged = [value for value in values if value is not None]
        return {
            'total': len(values),
            'completed': len(judged),
            'rate': sum(judged) / len(judged) if judged else None,
        }

    return {
        'sample_size': 15,
        'support': rate(support),
        'actions': {key: rate(values) for key, values in action_points.items()},
    }
