"""Unit tests for secret redaction, untrusted envelope, and size cap."""

import json
import pathlib

import pytest

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.tools import safety


@pytest.mark.parametrize(
    ('secret_label', 'secret_sample'),
    [
        pytest.param('openai', 'key sk-proj-abcd1234efgh5678ijkl', id='openai'),
        pytest.param('aws', 'id AKIAIOSFODNN7EXAMPLE', id='aws_key'),
        pytest.param(
            'gcp', 'AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGK000', id='gcp_key'
        ),
        pytest.param(
            'github', 'ghp_1234567890abcdef1234567890abcdef1234', id='github'
        ),
        pytest.param(
            'bearer', 'Authorization: Bearer tok_1234567890abcdef', id='bearer'
        ),
        pytest.param(
            'jwt',
            'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig_1234567890ab',
            id='jwt',
        ),
        pytest.param(
            'pem',
            '-----BEGIN RSA PRIVATE KEY-----\nabc\n'
            '-----END RSA PRIVATE KEY-----',
            id='pem',
        ),
        pytest.param(
            'kv_password', 'password=SuperSecretValue123', id='kv_password'
        ),
    ],
)
def test_redact_secrets_masks_planted_credentials(
    secret_label: str,
    secret_sample: str,
) -> None:
    """Common credential formats are replaced with [REDACTED_SECRET]."""
    _ = secret_label
    payload = {'log': f'Request failed with {secret_sample} in header'}

    cleaned, count = safety.redact_secrets(payload)

    assert count >= 1
    serialised = json.dumps(cleaned)
    assert '[REDACTED_SECRET]' in serialised
    assert secret_sample not in serialised


def test_registry_redacts_secrets_from_ingested_log_and_reports_count(
    tmp_path: pathlib.Path,
) -> None:
    """Tool outputs mask fake secrets stored inside ingested log files."""
    src = tmp_path / 'case'
    src.mkdir()
    fake_tok = 'sk-proj-test1234567890abcdef12'
    (src / 'app.log').write_text(
        f'2026-01-01T00:00:00Z ERROR api auth failed api_key={fake_tok}\n',
        encoding='utf-8',
    )

    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('search_text', {'table': 'app', 'pattern': 'failed'})

        assert res['ok'] is True
        assert int(str(res['redacted_secrets_count'])) >= 1
        dumped = json.dumps(res)
        assert fake_tok not in dumped
        assert '[REDACTED_SECRET]' in dumped
        assert reg.call_log[-1].redacted_secrets_count >= 1
    finally:
        ingested.close()


def test_cap_result_size_truncates_large_collections_under_6000_chars() -> None:
    """Oversized results are trimmed within max_chars with explicit notice."""
    rows = [{'idx': i, 'msg': 'x' * 80} for i in range(200)]
    payload = {
        'ok': True,
        'untrusted_data': safety.wrap_untrusted_data({'rows': rows}),
    }

    capped = safety.cap_result_size(payload, max_chars=6_000)
    dumped = json.dumps(capped, ensure_ascii=False)

    assert len(dumped) <= 6_000
    assert capped['truncated'] is True
    assert 'truncation_notice' in capped


def test_cap_result_size_truncates_single_oversized_string() -> None:
    """A single giant text blob still fits cleanly within max_chars."""
    payload = {
        'ok': True,
        'untrusted_data': safety.wrap_untrusted_data(
            {'text': 'a' * 10_000, 'lines': ['b' * 9_000]}
        ),
    }

    capped = safety.cap_result_size(payload, max_chars=6_000)
    cleaned_tuple, tuple_cnt = safety.redact_secrets(
        ('sk-proj-1234567890abcdef12',)
    )

    assert len(json.dumps(capped, ensure_ascii=False)) <= 6_000
    assert capped['truncated'] is True
    assert tuple_cnt == 1
    assert cleaned_tuple == ['[REDACTED_SECRET]']
