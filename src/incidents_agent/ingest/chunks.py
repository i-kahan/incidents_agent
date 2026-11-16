"""Structure-aware chunking for Markdown, prose, diffs, and config files."""

import json
import re

from incidents_agent.ingest import schema

_ATX_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
_HUNK_HEADER_RE = re.compile(r'^(@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@.*)$')
_DIFF_GIT_RE = re.compile(r'^diff --git\s+a/\S+\s+b/(\S+)')
_DIFF_PLUS_RE = re.compile(r'^\+\+\+\s+(?:b/)?(\S+)')
_INI_TOML_SECTION_RE = re.compile(r'^[ \t]*\[([^\]]+)\]\s*(?:[#;].*)?$')
_YAML_TOP_KEY_RE = re.compile(r'^([A-Za-z0-9_.-]+)\s*:')
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+|\n+')


def _hard_slice_with_overlap(
    text: str, max_chars: int, overlap_chars: int
) -> list[str]:
    """Slice a single long string into bounded pieces with overlap."""
    pieces: list[str] = []
    step_overlap = min(overlap_chars, max(0, max_chars // 4))
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + max_chars)
        if end < length:
            space_idx = text.rfind(' ', start + (max_chars // 2), end)
            if space_idx > start:
                end = space_idx
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= length:
            break
        next_start = max(start + 1, end - step_overlap)
        start = next_start
    return pieces


def split_oversized_text(
    text: str, max_chars: int, overlap_chars: int
) -> list[str]:
    """Split text exceeding max_chars at sentence boundaries with overlap.

    Args:
        text: Input string to split.
        max_chars: Target maximum characters per output chunk.
        overlap_chars: Desired character overlap between adjacent chunks.

    Returns:
        List of non-empty text chunks within `max_chars` when possible.
    """
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= max_chars:
        return [stripped]

    sentences = [
        part.strip()
        for part in _SENTENCE_SPLIT_RE.split(stripped)
        if part.strip()
    ]
    if not sentences:
        return _hard_slice_with_overlap(stripped, max_chars, overlap_chars)

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(' '.join(current))
                current = []
                current_len = 0
            chunks.extend(
                _hard_slice_with_overlap(sentence, max_chars, overlap_chars)
            )
            continue

        added_len = len(sentence) + (1 if current else 0)
        if current and (current_len + added_len) > max_chars:
            chunks.append(' '.join(current))
            overlap_items: list[str] = []
            overlap_len = 0
            for prev in reversed(current):
                next_len = overlap_len + len(prev) + (1 if overlap_items else 0)
                if next_len > overlap_chars and overlap_items:
                    break
                if len(prev) + len(sentence) + 2 > max_chars:
                    break
                overlap_items.append(prev)
                overlap_len = next_len
            overlap_items.reverse()
            current = overlap_items + [sentence]
            current_len = sum(len(s) for s in current) + max(
                0, len(current) - 1
            )
        else:
            current.append(sentence)
            current_len += added_len

    if current:
        chunks.append(' '.join(current))
    return chunks


def _pack_units(
    units: list[tuple[str, str]],
    doc_id: str,
    limits: schema.IngestLimits,
) -> list[schema.DocumentChunk]:
    """Pack raw (heading_path, text) units into bounded DocumentChunks."""
    chunks: list[schema.DocumentChunk] = []
    pos = 0
    for heading_path, raw_text in units:
        cleaned = raw_text.strip()
        if not cleaned:
            continue
        pieces = split_oversized_text(
            cleaned,
            max_chars=limits.max_chunk_chars,
            overlap_chars=limits.chunk_overlap_chars,
        )
        for piece in pieces:
            chunks.append(
                schema.DocumentChunk(
                    doc_id=doc_id,
                    chunk_id=f'{doc_id}::chunk_{pos:04d}',
                    position=pos,
                    heading_path=heading_path,
                    text=piece,
                )
            )
            pos += 1
    return chunks


def _chunk_paragraphs(title: str, text: str) -> list[tuple[str, str]]:
    """Split prose into paragraph-level (heading_path, text) pairs."""
    paragraphs = [
        para.strip() for para in re.split(r'\n\s*\n', text) if para.strip()
    ]
    if not paragraphs:
        return []
    return [
        (f'{title} :: paragraph {idx + 1}', para)
        for idx, para in enumerate(paragraphs)
    ]


def _chunk_headings(
    title: str, text: str, limits: schema.IngestLimits
) -> list[tuple[str, str]]:
    """Split Markdown/prose by heading hierarchy, then by paragraph if long."""
    lines = text.splitlines()
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []
    buffer: list[str] = []
    current_path = title
    saw_heading = False

    for line in lines:
        match = _ATX_HEADING_RE.match(line)
        if match:
            saw_heading = True
            body = '\n'.join(buffer).strip()
            if body:
                sections.append((current_path, body))
            buffer = [line]
            level = len(match.group(1))
            heading_text = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading_text))
            current_path = ' > '.join(item[1] for item in stack)
        else:
            buffer.append(line)

    trailing = '\n'.join(buffer).strip()
    if trailing:
        sections.append((current_path, trailing))

    if not saw_heading:
        return _chunk_paragraphs(title, text)

    expanded: list[tuple[str, str]] = []
    for path, body in sections:
        if len(body) <= limits.max_chunk_chars:
            expanded.append((path, body))
            continue
        paras = [p.strip() for p in re.split(r'\n\s*\n', body) if p.strip()]
        for para in paras:
            expanded.append((path, para))
    return expanded


def _chunk_hunks(title: str, text: str) -> list[tuple[str, str]]:
    """Split unified or git diffs by file and @@ hunk header."""
    units: list[tuple[str, str]] = []
    current_file = title
    file_preamble: list[str] = []
    current_hunk_header: str | None = None
    hunk_lines: list[str] = []

    def flush_hunk() -> None:
        nonlocal current_hunk_header, hunk_lines
        if current_hunk_header is not None and hunk_lines:
            heading = f'{current_file} :: {current_hunk_header}'
            units.append((heading, '\n'.join(hunk_lines).strip()))
        current_hunk_header = None
        hunk_lines = []

    for line in text.splitlines():
        git_match = _DIFF_GIT_RE.match(line)
        if git_match:
            flush_hunk()
            current_file = git_match.group(1)
            file_preamble = [line]
            continue

        plus_match = _DIFF_PLUS_RE.match(line)
        if plus_match and current_hunk_header is None:
            candidate = plus_match.group(1)
            if candidate != '/dev/null':
                current_file = candidate
            file_preamble.append(line)
            continue

        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            flush_hunk()
            current_hunk_header = hunk_match.group(1).strip()
            hunk_lines = list(file_preamble)
            hunk_lines.append(line)
            file_preamble = []
            continue

        if current_hunk_header is not None:
            hunk_lines.append(line)
        else:
            file_preamble.append(line)

    flush_hunk()
    if units:
        return units
    return _chunk_paragraphs(title, text)


def _chunk_sections(title: str, text: str) -> list[tuple[str, str]]:
    """Split JSON, INI/TOML, or YAML configs by top-level section."""
    stripped = text.strip()
    if stripped.startswith('{'):
        try:
            loaded: object = json.loads(stripped)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            return [
                (
                    f'{title} :: {key}',
                    json.dumps({str(key): val}, indent=2, ensure_ascii=False),
                )
                for key, val in loaded.items()
            ]

    lines = text.splitlines()
    if any(_INI_TOML_SECTION_RE.match(line) for line in lines):
        return _split_by_header_regex(title, lines, _INI_TOML_SECTION_RE)

    if any(_YAML_TOP_KEY_RE.match(line) for line in lines):
        return _split_by_header_regex(title, lines, _YAML_TOP_KEY_RE)

    return _chunk_paragraphs(title, text)


def _split_by_header_regex(
    title: str,
    lines: list[str],
    pattern: re.Pattern[str],
) -> list[tuple[str, str]]:
    """Split lines into top-level sections whenever pattern matches."""
    units: list[tuple[str, str]] = []
    current_label = f'{title} :: header'
    buffer: list[str] = []

    for line in lines:
        match = pattern.match(line)
        if match:
            body = '\n'.join(buffer).strip()
            if body:
                units.append((current_label, body))
            current_label = f'{title} :: {match.group(1).strip()}'
            buffer = [line]
        else:
            buffer.append(line)

    trailing = '\n'.join(buffer).strip()
    if trailing:
        units.append((current_label, trailing))
    return units


def derive_document_title(relative_path: str, text: str) -> str:
    """Derive a readable document title from the first Markdown H1 or path."""
    for line in text.splitlines()[:20]:
        match = _ATX_HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return relative_path


def chunk_document(
    doc_id: str,
    title: str,
    text: str,
    strategy: schema.ChunkStrategy,
    limits: schema.IngestLimits | None = None,
) -> list[schema.DocumentChunk]:
    """Split a document into structure-preserving, size-bounded chunks.

    Args:
        doc_id: Stable document identifier.
        title: Human-readable title for the document.
        text: Full UTF-8 document text.
        strategy: Chunking strategy ('whole', 'headings', 'paragraphs',
            'hunks', or 'sections').
        limits: Optional chunking size and overlap limits.

    Returns:
        Ordered list of DocumentChunk objects.
    """
    effective_limits = limits or schema.IngestLimits()
    cleaned = text.strip()
    if not cleaned:
        return []

    if strategy == 'whole':
        units = [(title, cleaned)]
    elif strategy == 'headings':
        units = _chunk_headings(title, cleaned, effective_limits)
    elif strategy == 'paragraphs':
        units = _chunk_paragraphs(title, cleaned)
    elif strategy == 'hunks':
        units = _chunk_hunks(title, cleaned)
    elif strategy == 'sections':
        units = _chunk_sections(title, cleaned)
    else:
        units = [(title, cleaned)]

    return _pack_units(units, doc_id=doc_id, limits=effective_limits)
