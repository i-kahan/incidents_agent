"""Deterministic file classification and optional model-assisted fallback."""

import hashlib
import json
import pathlib
import re

from incidents_agent.ingest import schema

_TABULAR_EXTS = frozenset({'.csv', '.tsv', '.parquet', '.jsonl', '.ndjson'})
_DIFF_EXTS = frozenset({'.diff', '.patch'})
_CONFIG_EXTS = frozenset({'.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf'})
_PROSE_EXTS = frozenset({'.md', '.markdown', '.rst', '.txt'})
_SUPPORTED_UNKNOWN_EXTS = frozenset({'', '.out', '.err', '.trace'})
_KNOWN_BINARY_EXTS = frozenset(
    {
        '.png',
        '.jpg',
        '.jpeg',
        '.gif',
        '.webp',
        '.pdf',
        '.zip',
        '.gz',
        '.tar',
        '.tgz',
        '.bz2',
        '.xz',
        '.exe',
        '.bin',
        '.so',
        '.dylib',
        '.dll',
        '.db',
        '.duckdb',
        '.sqlite',
    }
)

_KIND_MAP: dict[str, schema.FileKind] = {
    'tabular': 'tabular',
    'log': 'log',
    'document': 'document',
}
_DOC_STRATEGY_MAP: dict[str, schema.ChunkStrategy] = {
    'headings': 'headings',
    'paragraphs': 'paragraphs',
    'hunks': 'hunks',
    'sections': 'sections',
    'whole': 'whole',
}

_LOG_LINE_RE = re.compile(
    r'^(?:'
    r'\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?\]?'
    r'|\[?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    r'\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\]?'
    r'|\[?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]?'
    r'|\[?\d{10,19}(?:\.\d+)?\]?'
    r'|\[?(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|CRIT)'
    r'\]?(?:[:\s\]-])'
    r')',
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r'^#{1,6}\s+\S', re.MULTILINE)
_HUNK_RE = re.compile(
    r'^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@', re.MULTILINE
)
_DIFF_GIT_RE = re.compile(r'^diff --git\s+', re.MULTILINE)
_DIFF_OLD_RE = re.compile(r'^---\s+\S+', re.MULTILINE)
_DIFF_NEW_RE = re.compile(r'^\+\+\+\s+\S+', re.MULTILINE)


class UnsupportedFileError(Exception):
    """Raised when a file is binary, unreadable, or has an unsupported type."""


def compute_file_hash(path: pathlib.Path) -> str:
    """Compute the SHA-256 hex digest of a file using buffered reads."""
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_sample(path: pathlib.Path, max_bytes: int = 65536) -> str:
    """Read a UTF-8 sample and reject binary payloads containing NUL bytes."""
    raw = path.read_bytes()[:max_bytes]
    if b'\x00' in raw:
        raise UnsupportedFileError('binary content detected')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise UnsupportedFileError(f'non-UTF-8 file: {exc}') from exc


def _looks_like_diff(text: str) -> bool:
    """Return True when text contains unified or git diff markers."""
    if _DIFF_GIT_RE.search(text):
        return True
    return bool(
        _DIFF_OLD_RE.search(text)
        and _DIFF_NEW_RE.search(text)
        and _HUNK_RE.search(text)
    )


def _log_line_share(text: str) -> float:
    """Return the fraction of non-empty lines that begin like log lines."""
    lines = [line.strip() for line in text.splitlines()[:200] if line.strip()]
    if not lines:
        return 0.0
    matched = sum(1 for line in lines if _LOG_LINE_RE.match(line))
    return matched / len(lines)


def _classify_json(
    path: pathlib.Path, size_bytes: int, limits: schema.IngestLimits
) -> tuple[schema.FileKind, schema.ChunkStrategy, float, str]:
    """Inspect a .json file to distinguish record tables from config objects."""
    text = _read_text_sample(path, max_bytes=max(limits.max_file_bytes, 65536))
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnsupportedFileError(f'malformed JSON: {exc}') from exc
    if isinstance(parsed, list):
        return (
            'tabular',
            'none',
            0.90,
            'JSON array of records detected by content inspection',
        )
    if isinstance(parsed, dict):
        strategy: schema.ChunkStrategy = (
            'whole' if size_bytes <= limits.whole_doc_max_chars else 'sections'
        )
        return (
            'document',
            strategy,
            0.90,
            'JSON object detected; treated as sectioned document',
        )
    return (
        'document',
        'whole',
        0.75,
        'Scalar JSON file detected; kept as whole document',
    )


def _classify_text_content(
    ext: str,
    sample: str,
    size_bytes: int,
    limits: schema.IngestLimits,
) -> tuple[schema.FileKind, schema.ChunkStrategy, float, str]:
    """Classify text files using diff markers, log line share, and headings."""
    if _looks_like_diff(sample):
        return ('document', 'hunks', 0.90, 'Unified diff markers detected')

    log_share = _log_line_share(sample)
    if log_share >= 0.50:
        return (
            'log',
            'none',
            0.90,
            f'{log_share:.0%} of sampled lines match timestamp/level pattern',
        )
    if log_share >= 0.25:
        return (
            'log',
            'none',
            0.55,
            f'Ambiguous log-line share ({log_share:.0%}) in text file',
        )

    has_headings = bool(_HEADING_RE.search(sample))
    is_small = size_bytes <= limits.whole_doc_max_chars
    if ext in ('.md', '.markdown', '.rst'):
        strategy: schema.ChunkStrategy = 'whole' if is_small else 'headings'
        return (
            'document',
            strategy,
            0.92,
            f'Document extension {ext} with structured prose rules',
        )
    if has_headings:
        strategy = 'whole' if is_small else 'headings'
        return ('document', strategy, 0.85, 'Markdown headings detected')

    strategy = 'whole' if is_small else 'paragraphs'
    return (
        'document',
        strategy,
        0.55,
        'Plain text without headings or log prefixes (ambiguous)',
    )


def _classify_deterministic(
    path: pathlib.Path,
    relative_path: str,
    size_bytes: int,
    limits: schema.IngestLimits,
) -> tuple[schema.FileKind, schema.ChunkStrategy, float, str]:
    """Apply deterministic extension and content classification rules."""
    ext = path.suffix.lower()
    if ext in _KNOWN_BINARY_EXTS:
        raise UnsupportedFileError(f'unsupported file extension: {ext}')

    if ext == '.parquet':
        return ('tabular', 'none', 0.98, 'Parquet tabular extension')

    if ext in _TABULAR_EXTS:
        _ = _read_text_sample(path, max_bytes=8192)
        return ('tabular', 'none', 0.95, f'Tabular extension {ext}')

    if ext == '.json':
        return _classify_json(path, size_bytes, limits)

    if ext in _DIFF_EXTS:
        _ = _read_text_sample(path, max_bytes=8192)
        return ('document', 'hunks', 0.95, f'Diff extension {ext}')

    if ext in _CONFIG_EXTS:
        _ = _read_text_sample(path, max_bytes=8192)
        strategy: schema.ChunkStrategy = (
            'whole' if size_bytes <= limits.whole_doc_max_chars else 'sections'
        )
        return ('document', strategy, 0.90, f'Config extension {ext}')

    if ext == '.log':
        _ = _read_text_sample(path, max_bytes=8192)
        return ('log', 'none', 0.95, 'Log extension .log')

    if ext not in _PROSE_EXTS and ext not in _SUPPORTED_UNKNOWN_EXTS:
        # Unknown extensions must still be valid UTF-8 text to be inspected.
        sample = _read_text_sample(path, max_bytes=65536)
        return _classify_text_content(ext, sample, size_bytes, limits)

    sample = _read_text_sample(path, max_bytes=65536)
    return _classify_text_content(ext, sample, size_bytes, limits)


def _parse_menu_choice(
    kind: str, chunk_strategy: str
) -> tuple[schema.FileKind, schema.ChunkStrategy] | None:
    """Validate and coerce a model decision against the fixed menu."""
    typed_kind = _KIND_MAP.get(kind)
    if typed_kind is None:
        return None
    if typed_kind in ('tabular', 'log'):
        if chunk_strategy != 'none':
            return None
        return (typed_kind, 'none')
    typed_strategy = _DOC_STRATEGY_MAP.get(chunk_strategy)
    if typed_strategy is None:
        return None
    return (typed_kind, typed_strategy)


def _apply_model_fallback(
    path: pathlib.Path,
    relative_path: str,
    file_hash: str,
    size_bytes: int,
    rule_kind: schema.FileKind,
    rule_strategy: schema.ChunkStrategy,
    rule_confidence: float,
    rule_reason: str,
    config: schema.IngestConfig,
) -> schema.FileClassification:
    """Consult the optional model classifier when rule confidence is low."""
    cache = config.classifier_cache
    if cache is not None and file_hash in cache:
        cached = cache[file_hash]
        parsed_cached = _parse_menu_choice(cached.kind, cached.chunk_strategy)
        if parsed_cached is not None:
            cached_kind, cached_strategy = parsed_cached
            return schema.FileClassification(
                relative_path=relative_path,
                file_hash=file_hash,
                size_bytes=size_bytes,
                kind=cached_kind,
                chunk_strategy=cached_strategy,
                confidence=0.85,
                reason=f'cached model choice: {cached.reason}',
                used_model_fallback=True,
                model_cost_usd=0.0,
            )

    client = config.classifier_client
    if client is None:
        return schema.FileClassification(
            relative_path=relative_path,
            file_hash=file_hash,
            size_bytes=size_bytes,
            kind=rule_kind,
            chunk_strategy=rule_strategy,
            confidence=rule_confidence,
            reason=rule_reason,
        )

    sample = _read_text_sample(path, max_bytes=2000)
    decision = client.classify_file(relative_path, sample)
    parsed_choice = _parse_menu_choice(decision.kind, decision.chunk_strategy)
    if parsed_choice is None:
        return schema.FileClassification(
            relative_path=relative_path,
            file_hash=file_hash,
            size_bytes=size_bytes,
            kind=rule_kind,
            chunk_strategy=rule_strategy,
            confidence=rule_confidence,
            reason=(
                f'{rule_reason}; rejected out-of-menu model answer '
                f'({decision.kind}/{decision.chunk_strategy})'
            ),
            used_model_fallback=False,
            model_cost_usd=decision.cost_usd,
        )

    if cache is not None:
        cache[file_hash] = decision

    chosen_kind, chosen_strategy = parsed_choice
    return schema.FileClassification(
        relative_path=relative_path,
        file_hash=file_hash,
        size_bytes=size_bytes,
        kind=chosen_kind,
        chunk_strategy=chosen_strategy,
        confidence=0.85,
        reason=f'model fallback: {decision.reason}',
        used_model_fallback=True,
        model_cost_usd=decision.cost_usd,
    )


def classify_file(
    path: pathlib.Path,
    relative_path: str,
    config: schema.IngestConfig | None = None,
) -> schema.FileClassification:
    """Classify a single input file as tabular, log, or document.

    Args:
        path: Absolute or readable path to the input file.
        relative_path: Path relative to the ingested folder root.
        config: Optional ingest configuration for overrides, thresholds, and
            model-assisted fallback.

    Returns:
        FileClassification describing the decision, strategy, and provenance.

    Raises:
        UnsupportedFileError: When the file is binary, unreadable, or corrupt.
    """
    effective_config = config or schema.IngestConfig()
    size_bytes = path.stat().st_size
    file_hash = compute_file_hash(path)

    overrides = effective_config.classification_overrides
    if overrides is not None and relative_path in overrides:
        kind, strategy = overrides[relative_path]
        return schema.FileClassification(
            relative_path=relative_path,
            file_hash=file_hash,
            size_bytes=size_bytes,
            kind=kind,
            chunk_strategy=strategy,
            confidence=1.0,
            reason='explicit user classification override',
        )

    kind, strategy, confidence, reason = _classify_deterministic(
        path=path,
        relative_path=relative_path,
        size_bytes=size_bytes,
        limits=effective_config.limits,
    )

    if (
        effective_config.enable_model_classification
        and effective_config.classifier_client is not None
        and confidence < effective_config.classifier_confidence_threshold
    ):
        return _apply_model_fallback(
            path=path,
            relative_path=relative_path,
            file_hash=file_hash,
            size_bytes=size_bytes,
            rule_kind=kind,
            rule_strategy=strategy,
            rule_confidence=confidence,
            rule_reason=reason,
            config=effective_config,
        )

    return schema.FileClassification(
        relative_path=relative_path,
        file_hash=file_hash,
        size_bytes=size_bytes,
        kind=kind,
        chunk_strategy=strategy,
        confidence=confidence,
        reason=reason,
    )
