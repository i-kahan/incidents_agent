"""Unit tests for deterministic file classification and model fallback."""

import pathlib

import pytest

from incidents_agent.ingest import classifier
from incidents_agent.ingest import schema


class _RecordingFakeClassifier:
    """Fake model classifier recording calls and returning a fixed reply."""

    def __init__(self, decision: schema.ModelClassifierDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, str]] = []

    def classify_file(
        self, relative_path: str, sample: str
    ) -> schema.ModelClassifierDecision:
        self.calls.append((relative_path, sample))
        return self.decision


@pytest.mark.parametrize(
    ('filename', 'content', 'expected_kind', 'expected_strategy'),
    [
        pytest.param(
            'metrics.csv',
            'time,cpu\n1700000000,10.0\n',
            'tabular',
            'none',
            id='csv',
        ),
        pytest.param(
            'events.jsonl',
            '{"time": 1700000000, "msg": "ok"}\n',
            'tabular',
            'none',
            id='jsonl',
        ),
        pytest.param(
            'records.json',
            '[{"id": 1, "svc": "api"}]',
            'tabular',
            'none',
            id='json_array',
        ),
        pytest.param(
            'log_templates.json',
            '{"1": {"template": "start"}, "2": {"template": "stop"}}',
            'document',
            'whole',
            id='small_json_object',
        ),
        pytest.param(
            'service.log',
            '2026-01-01T00:00:00Z INFO ready\n',
            'log',
            'none',
            id='log_extension',
        ),
        pytest.param(
            'deploy.patch',
            '--- a/cfg.yaml\n+++ b/cfg.yaml\n@@ -1 +1 @@\n-a: 1\n+a: 2\n',
            'document',
            'hunks',
            id='patch_extension',
        ),
        pytest.param(
            'config.yaml',
            'server:\n  port: 8080\n' + ('  key: value\n' * 120),
            'document',
            'sections',
            id='large_yaml',
        ),
        pytest.param(
            'runbook.md',
            '# Incident Runbook\n\n## Recovery\n\n' + ('Check pool. ' * 120),
            'document',
            'headings',
            id='large_markdown',
        ),
    ],
)
def test_classify_file_deterministic_rules(
    tmp_path: pathlib.Path,
    filename: str,
    content: str,
    expected_kind: schema.FileKind,
    expected_strategy: schema.ChunkStrategy,
) -> None:
    """Extension and content rules classify standard file types accurately."""
    target = tmp_path / filename
    target.write_text(content, encoding='utf-8')

    result = classifier.classify_file(target, filename)

    assert result.kind == expected_kind
    assert result.chunk_strategy == expected_strategy
    assert result.used_model_fallback is False


def test_classify_file_misnamed_log_txt_classified_as_log(
    tmp_path: pathlib.Path,
) -> None:
    """A .txt file whose lines begin with timestamps/levels becomes a log."""
    lines = '\n'.join(
        [
            '2026-05-01T10:00:00Z INFO Starting worker',
            '2026-05-01T10:00:01Z WARN Queue depth rising',
            '2026-05-01T10:00:02Z ERROR Pool exhausted',
            '2026-05-01T10:00:03Z INFO Retrying connection',
        ]
    )
    target = tmp_path / 'worker_output.txt'
    target.write_text(lines, encoding='utf-8')

    result = classifier.classify_file(target, 'worker_output.txt')

    assert result.kind == 'log'
    assert result.chunk_strategy == 'none'


def test_classify_file_runbook_never_classified_as_log(
    tmp_path: pathlib.Path,
) -> None:
    """A runbook mentioning ERROR words in prose stays a document, not a log."""
    prose = (
        '# Database Recovery Runbook\n\n'
        'When investigating an incident, check pool saturation.\n'
        'Do not restart the primary node without draining connections.\n'
        'Contact the on-call engineer if errors persist.\n'
    )
    target = tmp_path / 'runbook.md'
    target.write_text(prose, encoding='utf-8')

    result = classifier.classify_file(target, 'runbook.md')

    assert result.kind == 'document'


def test_classify_file_diff_inside_txt_uses_hunks(
    tmp_path: pathlib.Path,
) -> None:
    """Unified diff markers in a .txt file trigger document/hunks strategy."""
    diff_text = (
        'diff --git a/app.py b/app.py\n'
        '--- a/app.py\n'
        '+++ b/app.py\n'
        '@@ -10,2 +10,2 @@\n'
        '-TIMEOUT = 2000\n'
        '+TIMEOUT = 5\n'
    )
    target = tmp_path / 'changes.txt'
    target.write_text(diff_text, encoding='utf-8')

    result = classifier.classify_file(target, 'changes.txt')

    assert result.kind == 'document'
    assert result.chunk_strategy == 'hunks'


def test_classify_file_fallback_disabled_makes_no_model_call(
    tmp_path: pathlib.Path,
) -> None:
    """With the fallback flag off, even ambiguous files never call the model."""
    target = tmp_path / 'notes.txt'
    target.write_text('Plain prose without headings.\n' * 60, encoding='utf-8')
    fake = _RecordingFakeClassifier(
        schema.ModelClassifierDecision('document', 'paragraphs', 'prose', 0.001)
    )
    cfg = schema.IngestConfig(
        enable_model_classification=False,
        classifier_client=fake,
    )

    result = classifier.classify_file(target, 'notes.txt', cfg)

    assert len(fake.calls) == 0
    assert result.used_model_fallback is False
    assert result.kind == 'document'


def test_classify_file_fallback_skipped_when_rules_confident(
    tmp_path: pathlib.Path,
) -> None:
    """Confident rule matches never invoke the model even when enabled."""
    target = tmp_path / 'metrics.csv'
    target.write_text('time,cpu\n1700000000,1\n', encoding='utf-8')
    fake = _RecordingFakeClassifier(
        schema.ModelClassifierDecision('tabular', 'none', 'csv', 0.001)
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=fake,
    )

    result = classifier.classify_file(target, 'metrics.csv', cfg)

    assert len(fake.calls) == 0
    assert result.kind == 'tabular'


def test_classify_file_fallback_uses_cache_and_runs_below_threshold(
    tmp_path: pathlib.Path,
) -> None:
    """Low-confidence file calls model once and reuses hash cache on second."""
    content = 'Unstructured incident note without headings.\n' * 60
    file_a = tmp_path / 'note_a.txt'
    file_b = tmp_path / 'note_b.txt'
    file_a.write_text(content, encoding='utf-8')
    file_b.write_text(content, encoding='utf-8')
    cache: dict[str, schema.ModelClassifierDecision] = {}
    fake = _RecordingFakeClassifier(
        schema.ModelClassifierDecision(
            'document', 'paragraphs', 'prose notes', 0.0005
        )
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=fake,
        classifier_cache=cache,
    )

    first = classifier.classify_file(file_a, 'note_a.txt', cfg)
    second = classifier.classify_file(file_b, 'note_b.txt', cfg)

    assert len(fake.calls) == 1
    assert first.used_model_fallback is True
    assert first.model_cost_usd == pytest.approx(0.0005)
    assert second.used_model_fallback is True
    assert second.model_cost_usd == 0.0


def test_classify_file_rejects_out_of_menu_model_answer(
    tmp_path: pathlib.Path,
) -> None:
    """An out-of-menu model response is rejected in favour of rules."""
    target = tmp_path / 'ambiguous.txt'
    target.write_text('Just some freeform text.\n' * 60, encoding='utf-8')
    fake = _RecordingFakeClassifier(
        schema.ModelClassifierDecision('spreadsheet', 'sliding_window', 'bad')
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=fake,
    )

    result = classifier.classify_file(target, 'ambiguous.txt', cfg)

    assert len(fake.calls) == 1
    assert result.used_model_fallback is False
    assert result.kind == 'document'
    assert result.chunk_strategy == 'paragraphs'
    assert 'rejected out-of-menu' in result.reason


def test_classify_file_respects_user_override(tmp_path: pathlib.Path) -> None:
    """Explicit classification_overrides bypass heuristics and fallback."""
    target = tmp_path / 'weird.txt'
    target.write_text('arbitrary content', encoding='utf-8')
    cfg = schema.IngestConfig(
        classification_overrides={'weird.txt': ('log', 'none')}
    )

    result = classifier.classify_file(target, 'weird.txt', cfg)

    assert result.kind == 'log'
    assert result.chunk_strategy == 'none'
    assert result.confidence == 1.0


@pytest.mark.parametrize(
    ('filename', 'payload'),
    [
        pytest.param('image.png', b'\x89PNG\r\n\x1a\n', id='binary_ext'),
        pytest.param('corrupt.txt', b'abc\x00def', id='nul_bytes'),
        pytest.param('bad_utf8.txt', b'\xff\xfe\xfd', id='invalid_utf8'),
        pytest.param('broken.json', b'{not: json', id='malformed_json'),
    ],
)
def test_classify_file_rejects_unsupported_or_corrupt_inputs(
    tmp_path: pathlib.Path,
    filename: str,
    payload: bytes,
) -> None:
    """Binary, non-UTF-8, and bad JSON inputs raise UnsupportedFileError."""
    target = tmp_path / filename
    target.write_bytes(payload)

    with pytest.raises(classifier.UnsupportedFileError):
        classifier.classify_file(target, filename)
