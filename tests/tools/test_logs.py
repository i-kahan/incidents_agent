"""Unit tests for search_text regex matching, masking, and time splits."""

import pathlib

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.tools import logs
from incidents_agent.tools import tables
from tests.tools import conftest


def test_search_text_masks_dynamic_ids_and_splits_counts(
    tmp_path: pathlib.Path,
) -> None:
    """search_text groups matching lines by masked template before/after."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'app.log').write_text(
        '2024-01-01T00:00:10Z INFO api ok req=111 ip=10.0.0.1\n'
        '2024-01-01T00:01:00Z ERROR api timeout req=112 ip=10.0.0.2\n'
        '2024-01-01T00:01:05Z ERROR api timeout req=113 ip=10.0.0.3\n'
        '2024-01-01T00:01:10Z ERROR api timeout '
        'id=123e4567-e89b-12d3-a456-426614174000\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'search_text',
            {
                'table': 'app',
                'pattern': 'timeout|ok',
                'split_time': 1704067230.0,
            },
        )

        assert res['ok'] is True
        matches = conftest.as_dict_list(
            conftest.untrusted_content(res)['matches']
        )
        templates = {str(m['pattern_template']): m for m in matches}
        timeout_key = '<TS> ERROR api timeout req=<NUM> ip=<IP>'
        assert timeout_key in templates
        assert templates[timeout_key]['total_count'] == 2
        assert templates[timeout_key]['count_before'] == 0
        assert templates[timeout_key]['count_after'] == 2
        assert any('<UUID>' in key for key in templates)
    finally:
        ingested.close()


def test_search_text_supports_explicit_column_and_python_regex_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """Explicit column lookup and lookahead regexes work cleanly."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'events.csv').write_text(
        'time,service,message\n'
        '1700000000,api,connection timeout\n'
        '1700000010,api,connection reset\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'search_text',
            {
                'table': 'events',
                'column': 'message',
                'pattern': 'connection (?=timeout)',
            },
        )
        bad_col = reg.call(
            'search_text',
            {'table': 'events', 'column': 'missing', 'pattern': 'timeout'},
        )
        bad_tbl = reg.call(
            'search_text',
            {'table': 'missing', 'pattern': 'timeout'},
        )

        assert res['ok'] is True
        assert conftest.as_dict(res['summary'])['total_matches'] == 1
        assert bad_col['ok'] is False
        assert conftest.as_dict(bad_col['error'])['code'] == 'unknown_column'
        assert bad_tbl['ok'] is False
        assert conftest.as_dict(bad_tbl['error'])['code'] == 'unknown_table'
    finally:
        ingested.close()


def test_search_text_refuses_split_time_without_usable_time(
    tmp_path: pathlib.Path,
) -> None:
    """search_text returns an error when split_time has no usable timestamps."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'clock_logs.csv').write_text(
        'time,message\n20:35,timeout error\n20:36,timeout error\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        without_split = reg.call(
            'search_text', {'table': 'clock_logs', 'pattern': 'timeout'}
        )
        with_split = reg.call(
            'search_text',
            {
                'table': 'clock_logs',
                'pattern': 'timeout',
                'split_time': 1700000000,
            },
        )

        assert without_split['ok'] is True
        assert with_split['ok'] is False
        assert conftest.as_dict(with_split['error'])['code'] == 'unusable_time'
    finally:
        ingested.close()


def test_search_text_handles_invalid_regex_and_empty_table(
    tmp_path: pathlib.Path,
) -> None:
    """Invalid regexes and empty log tables return clean structured replies."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'empty.log').write_text('', encoding='utf-8')
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        bad_regex = reg.call(
            'search_text', {'table': 'empty', 'pattern': '[unclosed'}
        )
        empty_search = reg.call(
            'search_text', {'table': 'empty', 'pattern': 'error'}
        )

        assert bad_regex['ok'] is False
        assert conftest.as_dict(bad_regex['error'])['code'] == 'invalid_regex'
        assert empty_search['ok'] is True
        assert conftest.as_dict(empty_search['summary'])['total_matches'] == 0
    finally:
        ingested.close()


def test_search_text_new_only_keeps_new_patterns_and_components(
    tmp_path: pathlib.Path,
) -> None:
    """`new_only` drops patterns seen before the split, before truncation."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'app.log').write_text(
        '2024-01-01T00:00:10Z INFO api ok req=111\n'
        '2024-01-01T00:00:20Z INFO api ok req=112\n'
        '2024-01-01T00:01:00Z INFO api ok req=113\n'
        '2024-01-01T00:01:05Z ERROR db deadlock req=114\n'
        '2024-01-01T00:01:10Z ERROR db deadlock req=115\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        ctx = tables.ToolContext(
            connection=ingested.connection,
            catalog=ingested.catalog,
            db_path=ingested.db_path,
        )
        args: dict[str, object] = {
            'table': 'app',
            'pattern': '.*',
            'split_time': 1704067230.0,
            'top_k': 1,
        }

        everything = logs.search_text(ctx, args)
        only_new = logs.search_text(ctx, args, new_only=True)
        no_split = logs.search_text(
            ctx, {'table': 'app', 'pattern': '.*'}, new_only=True
        )

        assert everything['ok'] is True
        assert only_new['ok'] is True
        matches = conftest.as_dict_list(
            conftest.untrusted_content(only_new)['matches']
        )
        assert [m['pattern_template'] for m in matches] == [
            '<TS> ERROR db deadlock req=<NUM>'
        ]
        assert matches[0]['count_before'] == 0
        assert matches[0]['count_after'] == 2
        assert no_split['ok'] is False
    finally:
        ingested.close()
