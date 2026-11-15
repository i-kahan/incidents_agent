"""Exercise RCAEval export, BARO, and scoring on a single synthetic case."""

import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import urllib.request

_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / 'tests'
    / 'fixtures'
    / 'synthetic_raw'
)
_BARO_EXAMPLE = (
    'https://raw.githubusercontent.com/phamquiluan/RCAEval/'
    '90b63a46ac1e969f1fa9015318b51e455c62f8c4/docs/examples/baro_agent.py'
)
_BARO_SHA256 = (
    '56381551080b26c840967bf491ca21b48534b12a81e16c17ac64fdf48e5a40af'
)


def export_fixture(workdir: pathlib.Path) -> pathlib.Path:
    """Export the hand-made case and return the blind task directory."""
    output = workdir / 'tasks'
    subprocess.run(
        [
            sys.executable,
            '-m',
            'RCAEval.agent',
            'export',
            '--dataset',
            're1-ob',
            '--data-root',
            str(_FIXTURE),
            '--out',
            str(output),
            '--copy',
        ],
        check=True,
    )
    return output / 're1-ob'


def score_fixture(
    tasks: pathlib.Path, predictions: pathlib.Path
) -> dict[str, object]:
    """Score synthetic predictions against the corresponding sibling answers."""
    answers = tasks.with_suffix('.answers.jsonl')
    summary = predictions.with_suffix('.summary.json')
    subprocess.run(
        [
            sys.executable,
            '-m',
            'RCAEval.agent',
            'evaluate',
            '--tasks',
            str(tasks),
            '--predictions',
            str(predictions),
            '--answers',
            str(answers),
            '--json-out',
            str(summary),
        ],
        check=True,
    )
    report: dict[str, object] = json.loads(summary.read_text(encoding='utf-8'))
    return report


def main() -> None:
    """Run the pinned BARO example and require a correct one-case score."""
    with tempfile.TemporaryDirectory(prefix='incidents-agent-smoke-') as tmp:
        workdir = pathlib.Path(tmp)
        tasks = export_fixture(workdir)
        example = workdir / 'baro_agent.py'
        predictions = workdir / 'predictions.jsonl'

        with urllib.request.urlopen(_BARO_EXAMPLE, timeout=30) as response:
            source = response.read()
        if hashlib.sha256(source).hexdigest() != _BARO_SHA256:
            sys.exit('The upstream BARO example failed checksum verification.')
        example.write_bytes(source)
        subprocess.run(
            [sys.executable, str(example), str(tasks), str(predictions)],
            check=True,
        )
        report = score_fixture(tasks, predictions)

        if report['n_cases'] != 1 or report['n_predicted'] != 1:
            sys.exit('Smoke check expected one scored prediction.')
        service = report['service']
        if not isinstance(service, dict) or service['AC@1'] != 1.0:
            sys.exit('Smoke check expected checkout as the top-ranked service.')
        print('BARO smoke check passed for the synthetic checkout case.')


if __name__ == '__main__':
    main()
