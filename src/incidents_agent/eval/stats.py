"""Seeded cluster statistics; official correctness comes from scoring."""

import dataclasses
import math
import random
import statistics
from collections.abc import Mapping
from collections.abc import Sequence

from incidents_agent.eval import scoring


@dataclasses.dataclass(frozen=True)
class Estimate:
    """A case-weighted mean and 95% percentile cluster interval."""

    value: float | None
    low: float | None
    high: float | None
    n_cases: int
    n_groups: int
    reason: str | None = None


def percentile(values: Sequence[float], fraction: float) -> float:
    """Return a linearly interpolated percentile of a nonempty sample."""
    if not values or not 0 <= fraction <= 1:
        raise ValueError('Percentile needs data and a fraction in [0, 1]')
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (
        position - lower
    )


def _unique(cases: Sequence[scoring.CaseScore]) -> dict[str, scoring.CaseScore]:
    result = {case.case_id: case for case in cases}
    if len(result) != len(cases) or not result:
        raise ValueError('Cases must be nonempty and unique')
    return result


def _aligned(
    first: Sequence[scoring.CaseScore],
    second: Sequence[scoring.CaseScore],
) -> list[tuple[scoring.CaseScore, scoring.CaseScore]]:
    a, b = _unique(first), _unique(second)
    if set(a) != set(b):
        raise ValueError('Paired comparison requires identical intended cases')
    pairs = [(a[key], b[key]) for key in sorted(a)]
    if any(x.group != y.group or x.indicator != y.indicator for x, y in pairs):
        raise ValueError('Paired labels or indicator eligibility differ')
    return pairs


def _bootstrap(
    groups: Mapping[tuple[str, str], list[float]],
    *,
    draws: int,
    seed: int,
) -> Estimate:
    if draws < 1:
        raise ValueError('draws must be positive')
    clusters = [groups[key] for key in sorted(groups)]
    values = [value for cluster in clusters for value in cluster]
    point = statistics.mean(values) if values else None
    if len(clusters) < 2:
        return Estimate(
            point,
            None,
            None,
            len(values),
            len(clusters),
            'fewer than two eligible groups',
        )
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [
            value
            for cluster in rng.choices(clusters, k=len(clusters))
            for value in cluster
        ]
        means.append(statistics.mean(sample))
    return Estimate(
        point,
        percentile(means, 0.025),
        percentile(means, 0.975),
        len(values),
        len(clusters),
    )


def accuracy(
    cases: Sequence[scoring.CaseScore],
    level: str,
    k: int,
    *,
    draws: int = 10_000,
    seed: int = 6,
) -> Estimate:
    """Bootstrap whole service-fault pairs, retaining case weighting."""
    _unique(cases)
    groups: dict[tuple[str, str], list[float]] = {}
    for case in cases:
        value = case.hit(level, k)
        if value is not None:
            groups.setdefault(case.group, []).append(value)
    return _bootstrap(groups, draws=draws, seed=seed)


def paired_difference(
    first: Sequence[scoring.CaseScore],
    second: Sequence[scoring.CaseScore],
    level: str,
    k: int,
    *,
    draws: int = 10_000,
    seed: int = 6,
) -> Estimate:
    """Return first minus second with shared cluster resampling."""
    groups: dict[tuple[str, str], list[float]] = {}
    for a, b in _aligned(first, second):
        av, bv = a.hit(level, k), b.hit(level, k)
        if av is not None and bv is not None:
            groups.setdefault(a.group, []).append(av - bv)
    return _bootstrap(groups, draws=draws, seed=seed)


def mcnemar(
    first: Sequence[scoring.CaseScore],
    second: Sequence[scoring.CaseScore],
    level: str = 'service',
) -> dict[str, object]:
    """Exact two-sided top-1 McNemar, secondary to clustered comparison."""
    wins = losses = 0
    for a, b in _aligned(first, second):
        av, bv = a.hit(level, 1), b.hit(level, 1)
        if av is not None and bv is not None:
            wins += int(av > bv)
            losses += int(av < bv)
    n = wins + losses
    p = min(
        1.0,
        2 * sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n,
    )
    return {
        'wins': wins,
        'losses': losses,
        'p_value': p,
        'caveat': 'secondary case-level test; repeats are not independent',
    }


def agreement(
    agent: Sequence[scoring.CaseScore],
    baseline: Sequence[scoring.CaseScore],
) -> dict[str, object]:
    """Split agent accuracy by top-service agreement; empties never agree."""
    agree: list[float] = []
    differ: list[float] = []
    for a, b in _aligned(agent, baseline):
        bucket = (
            agree
            if a.top_service and a.top_service == b.top_service
            else differ
        )
        bucket.append(float(a.hit('service', 1) or 0))
    return {
        'agreement': len(agree) / (len(agree) + len(differ)),
        'agree_n': len(agree),
        'differ_n': len(differ),
        'agree_accuracy': statistics.mean(agree) if agree else None,
        'differ_accuracy': statistics.mean(differ) if differ else None,
    }


def choose_baseline(
    baro: Sequence[scoring.CaseScore],
    guarded: Sequence[scoring.CaseScore],
) -> str:
    """Select on full tuning AC@1; caller persists this choice for reuse."""
    pairs = _aligned(baro, guarded)
    delta = sum(
        float(a.hit('service', 1) or 0) - float(b.hit('service', 1) or 0)
        for a, b in pairs
    )
    return 'baro' if delta > 0 else 'guarded_baro'


def noise(
    repeats: Sequence[Sequence[scoring.CaseScore]],
    fingerprints: Sequence[str],
) -> dict[str, object]:
    """Measure unordered-pair top-service disagreement and accuracy spread."""
    if len(repeats) < 2 or len(fingerprints) != len(repeats):
        raise ValueError('At least two identified repeats are required')
    if len(set(fingerprints)) != 1:
        raise ValueError('Repeat configurations differ')
    indexed = [_unique(repeat) for repeat in repeats]
    for repeat in repeats[1:]:
        _aligned(repeats[0], repeat)
    per_case = {}
    for case_id in sorted(indexed[0]):
        tops = [repeat[case_id].top_service for repeat in indexed]
        flips = sum(a != b for i, a in enumerate(tops) for b in tops[i + 1 :])
        per_case[case_id] = flips / math.comb(len(tops), 2)
    accuracies = [
        statistics.mean(float(c.hit('service', 1) or 0) for c in repeat)
        for repeat in repeats
    ]
    return {
        'per_case_flip_rate': per_case,
        'mean_flip_rate': statistics.mean(per_case.values()),
        'accuracies': accuracies,
        'accuracy_range': max(accuracies) - min(accuracies),
        'accuracy_stddev': statistics.pstdev(accuracies),
    }
