"""Free onset evaluation with explicit detection coverage."""

import statistics
import tempfile
from collections.abc import Mapping
from pathlib import Path

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.eval import stats


def summarize(errors: Mapping[str, float | None]) -> dict[str, object]:
    """Summarize absolute errors, retaining failed detections as misses."""
    if not errors:
        raise ValueError('Onset evaluation needs intended cases')
    detected = [value for value in errors.values() if value is not None]
    return {
        'n_cases': len(errors),
        'n_detected': len(detected),
        'detection_coverage': len(detected) / len(errors),
        'median_absolute_error_s': statistics.median(detected)
        if detected
        else None,
        'p90_absolute_error_s': stats.percentile(detected, 0.9)
        if detected
        else None,
        'within_60_seconds': sum(value <= 60 for value in detected)
        / len(errors),
    }


def evaluate(cases: Mapping[str, tuple[Path, float]]) -> dict[str, object]:
    """Call unchanged find_onset; known starts are used only for scoring."""
    errors: dict[str, float | None] = {}
    failures: dict[str, str] = {}
    for case_id, (folder, known_start) in cases.items():
        errors[case_id] = None
        try:
            with tempfile.TemporaryDirectory(prefix='eval-onset-') as work:
                loaded = ingest.ingest_folder(folder, workdir=Path(work))
                try:
                    result = tools.create_default_registry(loaded).call(
                        'find_onset',
                        {},
                    )
                    detected = result.get('estimated_onset_time_s')
                    if result.get('ok') and detected is not None:
                        errors[case_id] = abs(
                            float(str(detected)) - known_start
                        )
                finally:
                    loaded.close()
        except (ValueError, OSError, RuntimeError) as error:
            failures[case_id] = type(error).__name__
    return {**summarize(errors), 'errors_s': errors, 'failures': failures}
