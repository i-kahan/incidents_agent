"""Unit tests for secret redaction, untrusted envelope, and size cap."""

import json
import pathlib

import pytest

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.tools import safety
from tests.tools import conftest


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


def test_cap_result_size_trims_raw_data_before_response_structure() -> None:
    """A long untrusted list is cut before a short structural one."""
    tables_meta = [{'name': f'table_{i}', 'kind': 'metrics'} for i in range(7)]
    outline = [{'heading': f'Section {i}', 'line': i} for i in range(200)]
    payload = {
        'ok': True,
        'tables': tables_meta,
        'untrusted_data': safety.wrap_untrusted_data({'outline': outline}),
    }

    capped = safety.cap_result_size(payload, max_chars=2_000)

    assert len(json.dumps(capped, ensure_ascii=False)) <= 2_000
    assert capped['tables'] == tables_meta
    truncation = conftest.as_dict(capped['truncation'])
    lists = conftest.as_dict_list(truncation['lists'])
    assert lists[0]['path'] == 'untrusted_data.content.outline'
    assert lists[0]['total'] == 200
    kept = conftest.as_dict_list(conftest.untrusted_content(capped)['outline'])
    assert lists[0]['kept'] == len(kept)


def test_cap_result_size_trims_list_holding_most_characters() -> None:
    """The list with the most characters goes first, not the longest one."""
    many_small = list(range(300))
    few_large = ['y' * 400 for _ in range(10)]
    payload = {
        'ok': True,
        'untrusted_data': safety.wrap_untrusted_data(
            {'ids': many_small, 'blobs': few_large}
        ),
    }

    capped = safety.cap_result_size(payload, max_chars=3_000)

    content = conftest.untrusted_content(capped)
    assert content['ids'] == many_small
    assert isinstance(content['blobs'], list)
    assert len(content['blobs']) < 10


def test_cap_result_size_never_trims_summary_or_locator() -> None:
    """Summary and locator lists survive; truncation reports kept rows."""
    columns = [f'column_{i}' for i in range(40)]
    rows = [{'idx': i, 'msg': 'x' * 80} for i in range(200)]
    payload = {
        'ok': True,
        'summary': {'columns': columns, 'row_count': 200},
        'untrusted_data': safety.wrap_untrusted_data({'rows': rows}),
        'locator': {'kind': 'sql_query', 'columns': columns},
    }

    capped = safety.cap_result_size(payload, max_chars=6_000)

    assert len(json.dumps(capped, ensure_ascii=False)) <= 6_000
    assert conftest.as_dict(capped['summary'])['columns'] == columns
    assert conftest.as_dict(capped['locator'])['columns'] == columns
    kept_rows = conftest.as_dict_list(
        conftest.untrusted_content(capped)['rows']
    )
    truncation = conftest.as_dict(capped['truncation'])
    assert truncation['lists'] == [
        {
            'path': 'untrusted_data.content.rows',
            'kept': len(kept_rows),
            'total': 200,
        }
    ]
    assert kept_rows == rows[: len(kept_rows)]


def test_cap_result_size_leaves_small_results_unmarked() -> None:
    """A result under the cap gets truncated=False and no truncation key."""
    capped = safety.cap_result_size({'ok': True, 'rows': [1, 2]}, 6_000)

    assert capped['truncated'] is False
    assert 'truncation' not in capped
