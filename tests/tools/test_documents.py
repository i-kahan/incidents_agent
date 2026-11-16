"""Unit tests for search_documents and read_document."""

import pathlib

from incidents_agent import ingest
from incidents_agent import tools
from tests.tools import conftest


def _make_docs_db(tmp_path: pathlib.Path) -> ingest.IngestResult:
    src = tmp_path / 'docs_case'
    src.mkdir()
    runbook = (
        '# Service Runbook\n\n'
        '## Health Checks\n\n'
        'Inspect `/healthz` for pod liveness probes.\n\n'
        '## JWKS Timeout Mitigation\n\n'
        'When AUTH_JWKS_TIMEOUT_MS drops below 50ms, verification fails.\n'
        'Raise AUTH_JWKS_TIMEOUT_MS back to 2000ms.\n\n'
        '## Rollback Procedure\n\n'
        'Run kubectl rollout undo deployment/auth-service.\n'
    )
    # Add filler so runbook exceeds whole_doc_max_chars and splits headings.
    padding = 'Additional operational checklist notes.\n' * 45
    (src / 'runbook.md').write_text(
        f'{runbook}\n## Appendix\n\n{padding}', encoding='utf-8'
    )
    return ingest.ingest_folder(src, workdir=tmp_path / 'db')


def test_search_documents_ranks_matching_chunk_first_with_locator(
    tmp_path: pathlib.Path,
) -> None:
    """BM25 search finds relevant document chunks and attaches chunk_ids."""
    ingested = _make_docs_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'search_documents', {'query': 'AUTH_JWKS_TIMEOUT_MS token'}
        )

        assert res['ok'] is True
        hits = conftest.as_dict_list(conftest.untrusted_content(res)['results'])
        assert len(hits) >= 1
        assert 'JWKS Timeout Mitigation' in str(hits[0]['heading_path'])
        locator_chunks = conftest.as_dict(res['locator'])['chunk_ids']
        assert isinstance(locator_chunks, list)
        assert hits[0]['chunk_id'] in locator_chunks
    finally:
        ingested.close()


def test_read_document_returns_chunk_with_neighbors_and_outline(
    tmp_path: pathlib.Path,
) -> None:
    """read_document supports both chunk+neighbors and full document outline."""
    ingested = _make_docs_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)
        search = reg.call('search_documents', {'query': 'JWKS Timeout'})
        hits = conftest.as_dict_list(
            conftest.untrusted_content(search)['results']
        )
        target_id = str(hits[0]['chunk_id'])

        chunk_res = reg.call(
            'read_document', {'chunk_id': target_id, 'neighbors': 1}
        )
        outline_res = reg.call('read_document', {'doc_id': 'runbook.md'})

        assert chunk_res['ok'] is True
        chunks_out = conftest.as_dict_list(
            conftest.untrusted_content(chunk_res)['chunks']
        )
        assert len(chunks_out) == 3
        assert chunks_out[1]['chunk_id'] == target_id
        assert chunks_out[1]['is_target'] is True
        assert outline_res['ok'] is True
        outline = conftest.as_dict_list(
            conftest.untrusted_content(outline_res)['outline']
        )
        assert len(outline) >= 4
    finally:
        ingested.close()


def test_read_document_returns_error_on_unknown_ids(
    tmp_path: pathlib.Path,
) -> None:
    """Missing chunk_id or doc_id returns structured error dicts."""
    ingested = _make_docs_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        bad_chunk = reg.call('read_document', {'chunk_id': 'missing::chunk_0'})
        bad_doc = reg.call('read_document', {'doc_id': 'missing.md'})
        no_args = reg.call('read_document', {})

        assert bad_chunk['ok'] is False
        assert conftest.as_dict(bad_chunk['error'])['code'] == 'unknown_chunk'
        assert bad_doc['ok'] is False
        assert conftest.as_dict(bad_doc['error'])['code'] == 'unknown_document'
        assert no_args['ok'] is False
        assert conftest.as_dict(no_args['error'])['code'] == 'invalid_args'
    finally:
        ingested.close()
