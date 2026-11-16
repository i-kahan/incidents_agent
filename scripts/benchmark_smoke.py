"""Exercise RCAEval export, BARO, and scoring on a single synthetic case."""

import json
import pathlib
import subprocess
import sys
import tempfile

# RCAEval 1.12.0 ships neither type stubs nor a py.typed marker.
import RCAEval.agent.toolkit  # type: ignore[import-untyped]

_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1]
    / 'tests'
    / 'fixtures'
    / 'synthetic_raw'
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


def run_smoke(workdir: pathlib.Path) -> dict[str, object]:
    """Export and score the installed BARO implementation on one case."""
    tasks = export_fixture(workdir)
    manifest = json.loads((tasks / 'manifest.json').read_text(encoding='utf-8'))
    (case_id,) = manifest['case_ids']
    case = RCAEval.agent.toolkit.CaseToolkit(str(tasks / 'cases' / case_id))
    result = case.rank_anomalies(modality='metrics')
    ranks = [entry['name'] for entry in result['ranked']]
    predictions = workdir / 'predictions.jsonl'
    record = {'case_id': case_id, 'ranks': ranks, 'model': 'baro'}
    predictions.write_text(json.dumps(record) + '\n', encoding='utf-8')
    return score_fixture(tasks, predictions)


def main() -> None:
    """Run the offline smoke check and require a correct one-case score."""
    with tempfile.TemporaryDirectory(prefix='incidents-agent-smoke-') as tmp:
        report = run_smoke(pathlib.Path(tmp))

    if report['n_cases'] != 1 or report['n_predicted'] != 1:
        sys.exit('Smoke check expected one scored prediction.')
    service = report['service']
    if not isinstance(service, dict) or service['AC@1'] != 1.0:
        sys.exit('Smoke check expected checkout as the top-ranked service.')
    print('BARO smoke check passed for the synthetic checkout case.')


if __name__ == '__main__':
    main()
