"""Split benchmark cases into tuning and held-out sets by scenario.

RCAEval repeats every service-fault pair several times. Cases are grouped by
that pair and whole groups are assigned to one side, so no held-out scenario
has a repetition in the tuning set. The written split names groups by opaque
ids (`g01`, `g02`, ...) and never records a service or a fault, so it can be
committed without revealing labels.
"""

import dataclasses
import random
from collections.abc import Mapping
from collections.abc import Sequence

from incidents_agent.eval import answers as answers_lib

SCHEMA_VERSION = 1
ALGORITHM = 'pair-sample-with-coverage-v1'
_MAX_ATTEMPTS = 100_000


class SplitError(ValueError):
    """A split cannot be built, read or verified."""


@dataclasses.dataclass(frozen=True)
class Group:
    """The repetitions of one service-fault pair.

    The labels are kept in memory to check coverage; they are never written.

    Attributes:
        group_id: Opaque id, for example `g07`.
        case_ids: The cases in the group, sorted.
        service: The root-cause service of every case in the group.
        fault: The fault type of every case in the group.
    """

    group_id: str
    case_ids: tuple[str, ...]
    service: str
    fault: str


@dataclasses.dataclass(frozen=True)
class Split:
    """A locked tuning and held-out split.

    Attributes:
        dataset: The dataset name, for example `re2-ob`.
        seed: The random seed used to choose the tuning groups.
        attempt: How many draws the seed needed to meet coverage.
        answers_sha256: Digest of the answers file the split was built from.
        groups: Case ids per opaque group id.
        tuning_groups: Group ids on the tuning side, sorted.
        heldout_groups: Group ids on the held-out side, sorted.
        algorithm: The name of the procedure that built the split.
        schema_version: The version of the JSON layout.
    """

    dataset: str
    seed: int
    attempt: int
    answers_sha256: str
    groups: Mapping[str, tuple[str, ...]]
    tuning_groups: tuple[str, ...]
    heldout_groups: tuple[str, ...]
    algorithm: str = ALGORITHM
    schema_version: int = SCHEMA_VERSION

    @property
    def tuning(self) -> tuple[str, ...]:
        """Tuning case ids, sorted."""
        return self._cases(self.tuning_groups)

    @property
    def heldout(self) -> tuple[str, ...]:
        """Held-out case ids, sorted."""
        return self._cases(self.heldout_groups)

    def _cases(self, group_ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(sorted(c for g in group_ids for c in self.groups[g]))

    def to_json(self) -> dict[str, object]:
        """Return the split as a JSON-ready dictionary."""
        return {
            'schema_version': self.schema_version,
            'dataset': self.dataset,
            'algorithm': self.algorithm,
            'seed': self.seed,
            'attempt': self.attempt,
            'answers_sha256': self.answers_sha256,
            'groups': {g: list(c) for g, c in sorted(self.groups.items())},
            'tuning_groups': list(self.tuning_groups),
            'heldout_groups': list(self.heldout_groups),
            'tuning': list(self.tuning),
            'heldout': list(self.heldout),
        }


def group_pairs(
    answers: Sequence[answers_lib.Answer], group_size: int = 3
) -> list[Group]:
    """Group cases by service-fault pair.

    Groups are numbered by their smallest case id, so the ids carry no label.

    Args:
        answers: All labelled cases.
        group_size: The number of repetitions every pair must have.

    Returns:
        The groups, ordered by id.

    Raises:
        SplitError: A pair has the wrong number of repetitions.
    """
    by_pair: dict[tuple[str, str], list[str]] = {}
    for answer in answers:
        key = (answer.service, answer.fault)
        by_pair.setdefault(key, []).append(answer.case_id)
    sizes = {len(cases) for cases in by_pair.values()}
    if sizes != {group_size}:
        raise SplitError(
            f'every pair needs {group_size} cases; found sizes {sorted(sizes)}'
        )
    ordered = sorted(by_pair.items(), key=lambda item: min(item[1]))
    width = max(2, len(str(len(ordered))))
    return [
        Group(
            group_id=f'g{index:0{width}d}',
            case_ids=tuple(sorted(cases)),
            service=service,
            fault=fault,
        )
        for index, ((service, fault), cases) in enumerate(ordered, start=1)
    ]


def _covers(chosen: Sequence[Group], groups: Sequence[Group]) -> bool:
    services = {g.service for g in chosen}
    faults = {g.fault for g in chosen}
    return services == {g.service for g in groups} and faults == {
        g.fault for g in groups
    }


def choose_tuning(
    groups: Sequence[Group], seed: int, n_tuning: int
) -> tuple[tuple[str, ...], int]:
    """Draw tuning groups until every service and fault type is covered.

    Each attempt draws `n_tuning` groups uniformly without replacement from
    one seeded generator; the first draw that covers every service and every
    fault type is kept.

    Args:
        groups: All groups.
        seed: The random seed.
        n_tuning: How many groups go to tuning.

    Returns:
        The chosen group ids, sorted, and the attempt number that met
        coverage.

    Raises:
        SplitError: `n_tuning` is out of range, or no draw meets coverage.
    """
    if not 0 < n_tuning < len(groups):
        raise SplitError(f'n_tuning must be between 1 and {len(groups) - 1}')
    rng = random.Random(seed)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        chosen = rng.sample(list(groups), n_tuning)
        if _covers(chosen, groups):
            return tuple(sorted(g.group_id for g in chosen)), attempt
    raise SplitError(
        f'no draw of {n_tuning} groups covered every service and fault'
    )


def make_split(
    answers: Sequence[answers_lib.Answer],
    *,
    dataset: str,
    seed: int,
    n_tuning: int,
    answers_sha256: str,
) -> Split:
    """Build the split for a set of labelled cases.

    Args:
        answers: All labelled cases.
        dataset: The dataset name.
        seed: The random seed.
        n_tuning: How many groups go to tuning.
        answers_sha256: Digest of the answers file, recorded in the split.

    Returns:
        The split.
    """
    groups = group_pairs(answers)
    tuning, attempt = choose_tuning(groups, seed, n_tuning)
    heldout = tuple(g.group_id for g in groups if g.group_id not in tuning)
    return Split(
        dataset=dataset,
        seed=seed,
        attempt=attempt,
        answers_sha256=answers_sha256,
        groups={g.group_id: g.case_ids for g in groups},
        tuning_groups=tuning,
        heldout_groups=heldout,
    )


def _str_list(record: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = record.get(key)
    if not isinstance(value, list) or not all(
        isinstance(v, str) for v in value
    ):
        raise SplitError(f'{key} must be a list of strings')
    return tuple(value)


def from_json(record: Mapping[str, object]) -> Split:
    """Read a split written by `Split.to_json`.

    Args:
        record: The parsed JSON object.

    Returns:
        The split.

    Raises:
        SplitError: The record has the wrong schema or is inconsistent.
    """
    if record.get('schema_version') != SCHEMA_VERSION:
        raise SplitError(
            f'unsupported schema_version, expected {SCHEMA_VERSION}'
        )
    raw_groups = record.get('groups')
    if not isinstance(raw_groups, dict):
        raise SplitError('groups must be an object')
    groups = {str(g): _str_list(raw_groups, g) for g in raw_groups}
    dataset, algorithm = record.get('dataset'), record.get('algorithm')
    digest = record.get('answers_sha256')
    seed, attempt = record.get('seed'), record.get('attempt')
    if not (
        isinstance(dataset, str)
        and isinstance(algorithm, str)
        and isinstance(digest, str)
    ):
        raise SplitError('dataset, algorithm and answers_sha256 are required')
    if not (isinstance(seed, int) and isinstance(attempt, int)):
        raise SplitError('seed and attempt must be integers')
    split = Split(
        dataset=dataset,
        seed=seed,
        attempt=attempt,
        answers_sha256=digest,
        groups=groups,
        tuning_groups=_str_list(record, 'tuning_groups'),
        heldout_groups=_str_list(record, 'heldout_groups'),
        algorithm=algorithm,
    )
    sides = set(split.tuning_groups), set(split.heldout_groups)
    if sides[0] & sides[1] or sides[0] | sides[1] != set(groups):
        raise SplitError('every group must be on exactly one side')
    tuning, heldout = _str_list(record, 'tuning'), _str_list(record, 'heldout')
    if (tuning, heldout) != (split.tuning, split.heldout):
        raise SplitError('case lists do not match the groups')
    return split


def verify(
    split: Split,
    answers: Sequence[answers_lib.Answer],
    answers_sha256: str,
) -> None:
    """Check that a stored split is the one these answers produce.

    Args:
        split: The stored split.
        answers: All labelled cases.
        answers_sha256: Digest of the answers file they came from.

    Raises:
        SplitError: The answers file changed, or rebuilding the split from
            the stored seed gives a different result.
    """
    if answers_sha256 != split.answers_sha256:
        raise SplitError('the answers file differs from the one split')
    rebuilt = make_split(
        answers,
        dataset=split.dataset,
        seed=split.seed,
        n_tuning=len(split.tuning_groups),
        answers_sha256=answers_sha256,
    )
    if rebuilt != split:
        raise SplitError('rebuilding the split from its seed gives a change')
