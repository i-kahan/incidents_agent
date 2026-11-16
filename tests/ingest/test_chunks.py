"""Unit tests for structure-aware document chunking."""

from incidents_agent.ingest import chunks
from incidents_agent.ingest import schema


def test_chunk_document_preserves_markdown_heading_paths() -> None:
    """Markdown chunks preserve hierarchical H1 > H2 > H3 heading paths."""
    doc = (
        '# Service Guide\n\n'
        'Overview text.\n\n'
        '## Checkout\n\n'
        'Checkout intro.\n\n'
        '### Timeout\n\n'
        'Increase timeout when downstream latencies spike.\n'
    )

    result = chunks.chunk_document(
        doc_id='guide.md',
        title='Service Guide',
        text=doc,
        strategy='headings',
    )

    paths = [chunk.heading_path for chunk in result]
    assert paths == [
        'Service Guide',
        'Service Guide > Checkout',
        'Service Guide > Checkout > Timeout',
    ]
    assert [chunk.position for chunk in result] == [0, 1, 2]


def test_chunk_document_splits_diff_by_file_and_hunk() -> None:
    """Unified diffs produce one chunk per hunk tagged with file and header."""
    diff = (
        'diff --git a/auth.yaml b/auth.yaml\n'
        '--- a/auth.yaml\n'
        '+++ b/auth.yaml\n'
        '@@ -10,3 +10,3 @@\n'
        '-jwks_timeout_ms: 2000\n'
        '+jwks_timeout_ms: 5\n'
        '@@ -40,2 +40,2 @@\n'
        '-retries: 3\n'
        '+retries: 0\n'
    )

    result = chunks.chunk_document(
        doc_id='deploy.patch',
        title='deploy.patch',
        text=diff,
        strategy='hunks',
    )

    assert len(result) == 2
    assert result[0].heading_path == 'auth.yaml :: @@ -10,3 +10,3 @@'
    assert 'jwks_timeout_ms: 5' in result[0].text
    assert result[1].heading_path == 'auth.yaml :: @@ -40,2 +40,2 @@'
    assert 'retries: 0' in result[1].text


def test_chunk_document_splits_configs_by_top_level_section() -> None:
    """JSON, INI/TOML, and YAML configs chunk cleanly by top-level section."""
    yaml_cfg = (
        'database:\n'
        '  host: db.internal\n'
        '  pool_size: 20\n'
        'cache:\n'
        '  ttl_s: 300\n'
    )
    ini_cfg = '[server]\nport = 8080\n\n[worker]\nthreads = 4\n'
    json_cfg = '{"auth": {"timeout": 5}, "metrics": {"enabled": true}}'

    yaml_chunks = chunks.chunk_document(
        doc_id='cfg.yaml', title='cfg.yaml', text=yaml_cfg, strategy='sections'
    )
    ini_chunks = chunks.chunk_document(
        doc_id='cfg.ini', title='cfg.ini', text=ini_cfg, strategy='sections'
    )
    json_chunks = chunks.chunk_document(
        doc_id='cfg.json', title='cfg.json', text=json_cfg, strategy='sections'
    )

    assert [c.heading_path for c in yaml_chunks] == [
        'cfg.yaml :: database',
        'cfg.yaml :: cache',
    ]
    assert [c.heading_path for c in ini_chunks] == [
        'cfg.ini :: server',
        'cfg.ini :: worker',
    ]
    assert [c.heading_path for c in json_chunks] == [
        'cfg.json :: auth',
        'cfg.json :: metrics',
    ]


def test_chunk_document_keeps_small_document_whole() -> None:
    """Whole strategy keeps short documents in a single citable chunk."""
    doc = '# Short Note\n\nOnly a few words.'

    result = chunks.chunk_document(
        doc_id='short.md',
        title='Short Note',
        text=doc,
        strategy='whole',
    )

    assert len(result) == 1
    assert result[0].chunk_id == 'short.md::chunk_0000'
    assert result[0].text == doc


def test_chunk_document_splits_oversized_paragraph_with_overlap() -> None:
    """Oversized paragraphs split at sentence boundaries with small overlap."""
    sentences = [
        f'Sentence number {i} explains pool behavior.' for i in range(8)
    ]
    long_para = ' '.join(sentences)
    limits = schema.IngestLimits(max_chunk_chars=120, chunk_overlap_chars=45)

    result = chunks.chunk_document(
        doc_id='long.md',
        title='long.md',
        text=long_para,
        strategy='paragraphs',
        limits=limits,
    )

    assert len(result) >= 3
    assert all(len(chunk.text) <= 120 for chunk in result)
    # Overlap carries the tail sentence from chunk 0 into chunk 1.
    tail_sentence = result[0].text.split('. ')[-1]
    assert tail_sentence in result[1].text


def test_chunk_document_without_headings_falls_back_to_paragraphs() -> None:
    """A document chunked with headings strategy but no headings splits ¶."""
    prose = 'First incident observation.\n\nSecond incident observation.'

    result = chunks.chunk_document(
        doc_id='note.md',
        title='note.md',
        text=prose,
        strategy='headings',
    )

    assert len(result) == 2
    assert result[0].heading_path == 'note.md :: paragraph 1'
    assert result[1].heading_path == 'note.md :: paragraph 2'


def test_chunk_document_hard_slices_single_long_sentence_with_overlap() -> None:
    """A single sentence exceeding max_chunk_chars slices with overlap."""
    long_line = 'word ' * 60
    limits = schema.IngestLimits(max_chunk_chars=80, chunk_overlap_chars=20)

    result = chunks.chunk_document(
        doc_id='unpunctuated.txt',
        title='unpunctuated.txt',
        text=long_line,
        strategy='paragraphs',
        limits=limits,
    )

    assert len(result) >= 4
    assert all(len(chunk.text) <= 80 for chunk in result)


def test_derive_document_title_uses_first_h1_or_relative_path() -> None:
    """Title derivation picks the first H1 heading or falls back to path."""
    assert (
        chunks.derive_document_title('docs/a.md', '# My Runbook\n\nBody')
        == 'My Runbook'
    )
    assert chunks.derive_document_title('docs/b.txt', 'Plain text') == (
        'docs/b.txt'
    )
