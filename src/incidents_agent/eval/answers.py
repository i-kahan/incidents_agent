"""Read RCAEval answers files and write subsets of them.

An answers file holds the labels for a benchmark export, one JSON object per
line. It lives beside the blind task directory and never inside it, so the
agent cannot read it. This module keeps it that way for every subset it
writes.
"""

import dataclasses
import hashlib
import json
import pathlib
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence


class AnswersError(ValueError):
    """An answers file is malformed or would be written somewhere unsafe."""


@dataclasses.dataclass(frozen=True)
class Answer:
    """One labelled case.

    Attributes:
        case_id: The RCAEval case id, for example `re2-ob-0001`.
        service: The root-cause service.
        fault: The injected fault type.
        raw: The original JSON object, written back unchanged in subsets.
    """

    case_id: str
    service: str
    fault: str
    raw: Mapping[str, object]


def _parse_line(line: str, number: int) -> Answer:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise AnswersError(f'line {number}: not valid JSON') from error
    if not isinstance(record, dict):
        raise AnswersError(f'line {number}: expected a JSON object')
    fields = {}
    for key in ('case_id', 'service', 'fault'):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise AnswersError(f'line {number}: missing or empty {key}')
        fields[key] = value
    return Answer(
        case_id=fields['case_id'],
        service=fields['service'],
        fault=fields['fault'],
        raw=record,
    )


def load_answers(path: pathlib.Path) -> list[Answer]:
    """Load an answers file, keeping its line order.

    Args:
        path: The JSON lines answers file.

    Returns:
        One `Answer` per non-blank line.

    Raises:
        AnswersError: A line is malformed or a case id repeats.
    """
    answers = []
    seen = set()
    text = path.read_text(encoding='utf-8')
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        answer = _parse_line(line, number)
        if answer.case_id in seen:
            raise AnswersError(
                f'line {number}: duplicate case_id {answer.case_id!r}'
            )
        seen.add(answer.case_id)
        answers.append(answer)
    return answers


def file_sha256(path: pathlib.Path) -> str:
    """Return the hex SHA-256 digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def subset(answers: Sequence[Answer], case_ids: Iterable[str]) -> list[Answer]:
    """Select answers by case id, keeping the original order.

    Args:
        answers: All answers.
        case_ids: The case ids to keep.

    Returns:
        The selected answers.

    Raises:
        AnswersError: A requested case id has no answer.
    """
    wanted = set(case_ids)
    missing = wanted - {answer.case_id for answer in answers}
    if missing:
        raise AnswersError(f'no answer for {len(missing)} requested case(s)')
    return [answer for answer in answers if answer.case_id in wanted]


def write_answers(
    answers: Sequence[Answer],
    path: pathlib.Path,
    tasks_dir: pathlib.Path,
) -> None:
    """Write answers as JSON lines, never inside the blind task directory.

    Args:
        answers: The answers to write, in order.
        path: The output file.
        tasks_dir: The blind task directory the agent is given.

    Raises:
        AnswersError: `path` is inside `tasks_dir`.
    """
    if path.resolve().is_relative_to(tasks_dir.resolve()):
        raise AnswersError('answers must not be written inside the task view')
    lines = [json.dumps(answer.raw) + '\n' for answer in answers]
    path.write_text(''.join(lines), encoding='utf-8')
