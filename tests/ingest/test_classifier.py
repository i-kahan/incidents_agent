"""Unit tests for deterministic file classification and model fallback."""

import decimal
import json
import pathlib

import pytest

from incidents_agent.ingest import classifier
from incidents_agent.ingest import schema
from incidents_agent.llm import contracts
from incidents_agent.llm import fake


def _chat_result(text: str, cost_usd: str = '0.0005') -> contracts.ChatResult:
    """Build a deterministic ChatResult for ScriptedClient tests."""
    return contracts.ChatResult(
        response=contracts.ProviderResult(
            message=contracts.Message(role='assistant', text=text),
            usage=contracts.Usage(input_tokens=20, output_tokens=10),
            served_model='fake-classifier',
            request_id='req-1',
        ),
        reservation_id='res-1',
        started_at='1970-01-01T00:00:00+00:00',
        requested_model='fake-classifier',
        cost_usd=decimal.Decimal(cost_usd),
        limit_usd=decimal.Decimal('1.00'),
    )


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
    client = fake.ScriptedClient(
        [
            contracts.Message(
                'assistant',
                '{"kind": "document", "chunk_strategy": "paragraphs", '
                '"reason": "prose"}',
            )
        ]
    )
    cfg = schema.IngestConfig(
        enable_model_classification=False,
        classifier_client=client,
    )

    result = classifier.classify_file(target, 'notes.txt', cfg)

    assert len(client.requests) == 0
    assert result.used_model_fallback is False
    assert result.kind == 'document'


def test_classify_file_fallback_skipped_when_rules_confident(
    tmp_path: pathlib.Path,
) -> None:
    """Confident rule matches never invoke the model even when enabled."""
    target = tmp_path / 'metrics.csv'
    target.write_text('time,cpu\n1700000000,1\n', encoding='utf-8')
    client = fake.ScriptedClient(
        [
            contracts.Message(
                'assistant',
                '{"kind": "tabular", "chunk_strategy": "none", '
                '"reason": "csv"}',
            )
        ]
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
    )

    result = classifier.classify_file(target, 'metrics.csv', cfg)

    assert len(client.requests) == 0
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
    client = fake.ScriptedClient(
        [
            _chat_result(
                '{"kind": "document", "chunk_strategy": "paragraphs", '
                '"reason": "prose notes"}',
                cost_usd='0.0005',
            )
        ]
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
        classifier_cache=cache,
    )

    first = classifier.classify_file(file_a, 'note_a.txt', cfg)
    second = classifier.classify_file(file_b, 'note_b.txt', cfg)

    assert len(client.requests) == 1
    assert first.used_model_fallback is True
    assert first.model_cost_usd == decimal.Decimal('0.0005')
    assert second.used_model_fallback is True
    assert second.model_cost_usd == decimal.Decimal(0)


def test_classify_file_fallback_parses_fenced_json_response(
    tmp_path: pathlib.Path,
) -> None:
    """Markdown code fences around a valid JSON choice are stripped."""
    target = tmp_path / 'summary.txt'
    target.write_text('Ambiguous unlabelled note.\n' * 60, encoding='utf-8')
    client = fake.ScriptedClient(
        [
            _chat_result(
                '```json\n'
                '{"kind": "document", "chunk_strategy": "whole", '
                '"reason": "short summary"}\n'
                '```',
                cost_usd='0.0002',
            )
        ]
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
    )

    result = classifier.classify_file(target, 'summary.txt', cfg)

    assert result.used_model_fallback is True
    assert result.kind == 'document'
    assert result.chunk_strategy == 'whole'
    assert result.model_cost_usd == decimal.Decimal('0.0002')


def test_classify_file_fallback_bounds_untrusted_sample(
    tmp_path: pathlib.Path,
) -> None:
    """Fallback request encloses a bounded sample inside untrusted_sample."""
    target = tmp_path / 'large_note.txt'
    target.write_text('Unstructured text line.\n' * 200, encoding='utf-8')
    client = fake.ScriptedClient(
        [
            contracts.Message(
                'assistant',
                '{"kind": "document", "chunk_strategy": "paragraphs", '
                '"reason": "prose"}',
            )
        ]
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
        classifier_model_alias='cheap',
        classifier_run=contracts.Run('run-123'),
        classifier_case_id='case-9',
    )

    classifier.classify_file(target, 'docs/large_note.txt', cfg)

    req, run, case_id = client.requests[0]
    user_text = req.history[0].text
    assert user_text is not None
    payload = json.loads(user_text)
    assert req.model_alias == 'cheap'
    assert run == contracts.Run('run-123')
    assert case_id == 'case-9'
    assert 'raw file data' in req.system_prompt
    assert payload['relative_path'] == 'docs/large_note.txt'
    assert len(payload['untrusted_sample'].encode('utf-8')) <= 2000


def test_classify_file_rejects_out_of_menu_model_answer(
    tmp_path: pathlib.Path,
) -> None:
    """An out-of-menu model response is rejected in favour of rules."""
    target = tmp_path / 'ambiguous.txt'
    target.write_text('Just some freeform text.\n' * 60, encoding='utf-8')
    cache: dict[str, schema.ModelClassifierDecision] = {}
    client = fake.ScriptedClient(
        [
            _chat_result(
                '{"kind": "spreadsheet", "chunk_strategy": "sliding_window", '
                '"reason": "bad"}',
                cost_usd='0.0004',
            )
        ]
    )
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
        classifier_cache=cache,
    )

    result = classifier.classify_file(target, 'ambiguous.txt', cfg)

    assert len(client.requests) == 1
    assert result.used_model_fallback is False
    assert result.kind == 'document'
    assert result.chunk_strategy == 'paragraphs'
    assert result.model_cost_usd == decimal.Decimal('0.0004')
    assert cache == {}
    assert 'rejected out-of-menu' in result.reason


@pytest.mark.parametrize(
    'reply_text',
    [
        pytest.param('Not JSON at all', id='non_json'),
        pytest.param('["document", "paragraphs"]', id='json_array'),
        pytest.param('{"kind": "log"}', id='missing_strategy'),
    ],
)
def test_classify_file_rejects_malformed_model_answer(
    tmp_path: pathlib.Path,
    reply_text: str,
) -> None:
    """Malformed model replies keep rule defaults without caching."""
    target = tmp_path / 'ambiguous.txt'
    target.write_text('Just some freeform text.\n' * 60, encoding='utf-8')
    cache: dict[str, schema.ModelClassifierDecision] = {}
    client = fake.ScriptedClient([_chat_result(reply_text, cost_usd='0.0003')])
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
        classifier_cache=cache,
    )

    result = classifier.classify_file(target, 'ambiguous.txt', cfg)

    assert result.used_model_fallback is False
    assert result.kind == 'document'
    assert result.chunk_strategy == 'paragraphs'
    assert result.model_cost_usd == decimal.Decimal('0.0003')
    assert cache == {}
    assert 'rejected malformed model answer' in result.reason


@pytest.mark.parametrize(
    ('error', 'expected_halted'),
    [
        pytest.param(contracts.BudgetRefused('limit'), True, id='budget'),
        pytest.param(contracts.BillingUncertain('held'), True, id='billing'),
        pytest.param(
            contracts.ModelUnavailable('no key'), True, id='unavailable'
        ),
        pytest.param(contracts.ProviderError('bad req'), False, id='provider'),
        pytest.param(contracts.RetryDeferred(2.0), False, id='retry'),
    ],
)
def test_classify_file_fallback_degrades_on_client_error(
    tmp_path: pathlib.Path,
    error: Exception,
    expected_halted: bool,
) -> None:
    """Model and budget exceptions fall back cleanly to deterministic rules."""
    target = tmp_path / 'ambiguous.txt'
    target.write_text('Just some freeform text.\n' * 60, encoding='utf-8')
    client = fake.ScriptedClient([error])
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=client,
    )

    result = classifier.classify_file(target, 'ambiguous.txt', cfg)

    assert result.used_model_fallback is False
    assert result.kind == 'document'
    assert result.chunk_strategy == 'paragraphs'
    assert result.model_cost_usd == decimal.Decimal(0)
    assert result.fallback_halted is expected_halted
    assert 'model fallback unavailable' in result.reason


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
