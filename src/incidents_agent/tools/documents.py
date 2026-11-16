"""BM25 keyword search and chunk/outline reader over document_chunks."""

import collections
import math
import re

from incidents_agent.tools import safety
from incidents_agent.tools import schema as tool_schema
from incidents_agent.tools import tables

_TOKEN_RE = re.compile(r'[a-z0-9_]+')


def _tokenise(text: str) -> list[str]:
    """Lowercase alphanumeric tokeniser for BM25 scoring."""
    return _TOKEN_RE.findall(text.lower())


def _make_excerpt(text: str, query_tokens: set[str], max_len: int = 240) -> str:
    """Extract a short snippet centred near the first matched token."""
    if len(text) <= max_len:
        return text
    lower = text.lower()
    best_pos = -1
    for tok in query_tokens:
        idx = lower.find(tok)
        if idx != -1 and (best_pos == -1 or idx < best_pos):
            best_pos = idx
    if best_pos <= max_len // 3:
        return f'{text[: max_len - 3]}...'
    start = max(0, best_pos - (max_len // 3))
    end = min(len(text), start + max_len - 6)
    prefix = '...' if start > 0 else ''
    suffix = '...' if end < len(text) else ''
    return f'{prefix}{text[start:end]}{suffix}'


def search_documents(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Run offline Okapi BM25 keyword search across ingested document chunks."""
    raw_query = args.get('query')
    if not isinstance(raw_query, str) or not raw_query.strip():
        return tables.error_result(
            'invalid_args', 'query must be a non-empty string'
        )
    query_tokens = _tokenise(raw_query)
    if not query_tokens:
        return tables.error_result(
            'invalid_args', 'query contains no searchable tokens'
        )

    raw_top_k = args.get('top_k', 5)
    if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool):
        return tables.error_result('invalid_args', 'top_k must be an integer')
    top_k = max(1, min(raw_top_k, 25))

    rows = ctx.connection.execute(
        'SELECT doc_id, chunk_id, position, heading_path, text '
        'FROM document_chunks ORDER BY doc_id, position'
    ).fetchall()
    if not rows:
        return {
            'ok': True,
            'summary': {'total_chunks': 0, 'matched_chunks': 0},
            'untrusted_data': safety.wrap_untrusted_data({'results': []}),
            'locator': tool_schema.ToolLocator(
                kind='document_chunks',
                table='document_chunks',
                chunk_ids=(),
                expression=raw_query,
            ).to_dict(),
            'target_components': [],
        }

    docs_tokens: list[list[str]] = []
    term_freqs: list[collections.Counter[str]] = []
    doc_freq: collections.Counter[str] = collections.Counter()

    for doc_id, _chunk_id, _pos, heading_path, text in rows:
        combined = f'{doc_id} {heading_path} {heading_path} {text}'
        toks = _tokenise(combined)
        docs_tokens.append(toks)
        tf = collections.Counter(toks)
        term_freqs.append(tf)
        for unique_term in tf:
            doc_freq[unique_term] += 1

    num_docs = len(rows)
    avgdl = sum(len(t) for t in docs_tokens) / num_docs
    k1 = 1.5
    b = 0.75
    unique_query = list(dict.fromkeys(query_tokens))
    query_set = set(unique_query)

    scored: list[tuple[float, dict[str, object]]] = []
    for idx, (doc_id, chunk_id, position, heading_path, text) in enumerate(
        rows
    ):
        dl = len(docs_tokens[idx])
        if dl == 0:
            continue
        tf_map = term_freqs[idx]
        score = 0.0
        for term in unique_query:
            term_count = tf_map.get(term, 0)
            if term_count == 0:
                continue
            df = doc_freq[term]
            idf = math.log((num_docs - df + 0.5) / (df + 0.5) + 1.0)
            denom = term_count + k1 * (1.0 - b + b * (dl / max(avgdl, 1e-9)))
            score += idf * (term_count * (k1 + 1.0)) / denom

        if score <= 0.0:
            continue
        scored.append(
            (
                score,
                {
                    'chunk_id': str(chunk_id),
                    'doc_id': str(doc_id),
                    'position': int(position),
                    'heading_path': str(heading_path),
                    'score': round(score, 4),
                    'excerpt': _make_excerpt(str(text), query_set),
                },
            )
        )

    scored.sort(key=lambda pair: (-pair[0], str(pair[1]['chunk_id'])))
    top_items = [item for _, item in scored[:top_k]]
    chunk_ids = tuple(str(item['chunk_id']) for item in top_items)

    return {
        'ok': True,
        'summary': {
            'total_chunks': num_docs,
            'matched_chunks': len(scored),
            'returned_chunks': len(top_items),
        },
        'untrusted_data': safety.wrap_untrusted_data({'results': top_items}),
        'locator': tool_schema.ToolLocator(
            kind='document_chunks',
            table='document_chunks',
            chunk_ids=chunk_ids,
            expression=raw_query,
        ).to_dict(),
        'target_components': [],
    }


def read_document(
    ctx: tables.ToolContext, args: dict[str, object]
) -> dict[str, object]:
    """Read one chunk with neighbouring chunks, or a document's full outline."""
    chunk_id = args.get('chunk_id')
    doc_id = args.get('doc_id')
    raw_neighbors = args.get('neighbors', 1)
    if not isinstance(raw_neighbors, int) or isinstance(raw_neighbors, bool):
        return tables.error_result(
            'invalid_args', 'neighbors must be an integer'
        )
    neighbors = max(0, min(raw_neighbors, 5))

    if isinstance(chunk_id, str) and chunk_id.strip():
        target = ctx.connection.execute(
            'SELECT doc_id, position FROM document_chunks WHERE chunk_id = ?',
            [chunk_id],
        ).fetchone()
        if target is None:
            return tables.error_result(
                'unknown_chunk', f'Document chunk not found: {chunk_id}'
            )
        parent_doc = str(target[0])
        target_pos = int(target[1])
        rows = ctx.connection.execute(
            'SELECT doc_id, chunk_id, position, heading_path, text '
            'FROM document_chunks '
            'WHERE doc_id = ? AND position BETWEEN ? AND ? '
            'ORDER BY position',
            [parent_doc, target_pos - neighbors, target_pos + neighbors],
        ).fetchall()
        chunks_out = [
            {
                'chunk_id': str(r[1]),
                'doc_id': str(r[0]),
                'position': int(r[2]),
                'heading_path': str(r[3]),
                'text': str(r[4]),
                'is_target': str(r[1]) == chunk_id,
            }
            for r in rows
        ]
        chunk_ids = tuple(str(item['chunk_id']) for item in chunks_out)
        return {
            'ok': True,
            'summary': {
                'doc_id': parent_doc,
                'target_chunk_id': chunk_id,
                'returned_chunks': len(chunks_out),
            },
            'untrusted_data': safety.wrap_untrusted_data(
                {'chunks': chunks_out}
            ),
            'locator': tool_schema.ToolLocator(
                kind='document_chunks',
                table='document_chunks',
                chunk_ids=chunk_ids,
                expression=f'chunk_id={chunk_id},neighbors={neighbors}',
            ).to_dict(),
            'target_components': [],
        }

    if isinstance(doc_id, str) and doc_id.strip():
        doc_entry = ctx.catalog.get_document(doc_id)
        rows = ctx.connection.execute(
            'SELECT chunk_id, position, heading_path '
            'FROM document_chunks WHERE doc_id = ? ORDER BY position',
            [doc_id],
        ).fetchall()
        if doc_entry is None and not rows:
            return tables.error_result(
                'unknown_document', f'Document not found: {doc_id}'
            )
        outline = [
            {
                'chunk_id': str(r[0]),
                'position': int(r[1]),
                'heading_path': str(r[2]),
            }
            for r in rows
        ]
        chunk_ids = tuple(str(item['chunk_id']) for item in outline)
        title = doc_entry.title if doc_entry is not None else doc_id
        return {
            'ok': True,
            'summary': {
                'doc_id': doc_id,
                'chunk_count': len(outline),
            },
            'untrusted_data': safety.wrap_untrusted_data(
                {'doc_id': doc_id, 'title': title, 'outline': outline}
            ),
            'locator': tool_schema.ToolLocator(
                kind='document_outline',
                table='document_chunks',
                chunk_ids=chunk_ids,
                expression=f'doc_id={doc_id}',
            ).to_dict(),
            'target_components': [],
        }

    return tables.error_result(
        'invalid_args', 'Provide either chunk_id or doc_id'
    )
