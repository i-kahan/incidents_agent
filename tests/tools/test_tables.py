"""Unit tests for list_tables, describe_table, run_sql, and registry."""

import pathlib

import pytest

from incidents_agent import ingest
from incidents_agent import tools
from incidents_agent.ingest import schema as ingest_schema
from incidents_agent.tools import tables
from tests.tools import conftest


def _make_sample_db(tmp_path: pathlib.Path) -> ingest_schema.IngestResult:
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text(
        'time,cpu,mem\n'
        '1700000000,10.0,100\n'
        '1700000010,20.0,200\n'
        '1700000020,30.0,300\n'
        '1700000030,40.0,400\n',
        encoding='utf-8',
    )
    (src / 'empty.csv').write_text('time,val\n', encoding='utf-8')
    (src / 'notes.md').write_text('# Service Notes\n\nAll ok.\n', 'utf-8')
    return ingest.ingest_folder(src, workdir=tmp_path / 'db')


def test_list_tables_returns_catalog_and_locator(
    tmp_path: pathlib.Path,
) -> None:
    """list_tables summarises tables, documents, and catalog locator."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('list_tables', {})

        assert res['ok'] is True
        assert res['locator'] == {'kind': 'catalog'}
        assert len(reg.schemas()) == 10
        assert len(reg.call_log) == 1
        assert reg.call_log[0].call_id == 'call_0001'
    finally:
        ingested.close()


def test_describe_table_returns_stats_and_handles_empty_table(
    tmp_path: pathlib.Path,
) -> None:
    """describe_table returns numeric stats and works cleanly on empty table."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        filled = reg.call('describe_table', {'table': 'metrics'})
        empty = reg.call('describe_table', {'table': 'empty'})

        assert filled['ok'] is True
        assert filled['row_count'] == 4
        assert empty['ok'] is True
        assert empty['row_count'] == 0
    finally:
        ingested.close()


def test_run_sql_executes_select_and_with_queries_and_enforces_limit(
    tmp_path: pathlib.Path,
) -> None:
    """run_sql supports SELECT and CTE queries and enforces row limits."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        simple = reg.call(
            'run_sql',
            {
                'query': 'SELECT time, cpu FROM metrics ORDER BY time',
                'limit': 2,
            },
        )
        cte = reg.call(
            'run_sql',
            {
                'query': (
                    'WITH hi AS (SELECT cpu FROM metrics WHERE cpu > 15) '
                    'SELECT COUNT(*) AS cnt FROM hi;'
                )
            },
        )

        summary = conftest.as_dict(simple['summary'])
        cte_rows = conftest.as_dict_list(
            conftest.untrusted_content(cte)['rows']
        )

        assert simple['ok'] is True
        assert summary['row_count'] == 2
        assert summary['has_more'] is True
        assert cte['ok'] is True
        assert cte_rows == [{'cnt': 3}]
    finally:
        ingested.close()


def test_run_sql_allows_forbidden_words_inside_string_literals(
    tmp_path: pathlib.Path,
) -> None:
    """Keywords like DROP inside quoted strings are allowed in SELECT."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'run_sql',
            {'query': "SELECT 'DROP TABLE' AS label FROM metrics LIMIT 1"},
        )

        rows = conftest.as_dict_list(conftest.untrusted_content(res)['rows'])
        assert res['ok'] is True
        assert rows == [{'label': 'DROP TABLE'}]
    finally:
        ingested.close()


@pytest.mark.parametrize(
    'query',
    [
        pytest.param('DELETE FROM metrics', id='delete'),
        pytest.param('INSERT INTO metrics VALUES (1, 2, 3, 1)', id='insert'),
        pytest.param('DROP TABLE metrics', id='drop'),
        pytest.param('SELECT 1; DROP TABLE metrics', id='multi_statement'),
        pytest.param("COPY metrics TO 'out.csv'", id='copy'),
        pytest.param('PRAGMA version', id='pragma'),
        pytest.param("ATTACH 'other.db' AS o", id='attach'),
        pytest.param('SET enable_external_access = true', id='set_config'),
        pytest.param(
            "SELECT * FROM read_csv_auto('/etc/passwd')", id='read_csv'
        ),
        pytest.param('WITH x AS (SELECT 1) DELETE FROM metrics', id='with_del'),
        pytest.param("SELECT 'unterminated", id='unterminated_quote'),
        pytest.param('SELECT /* unterminated', id='unterminated_comment'),
        pytest.param('   ', id='blank'),
    ],
)
def test_run_sql_blocks_non_select_and_sandbox_escapes(
    tmp_path: pathlib.Path,
    query: str,
) -> None:
    """Non-SELECT, multi-statement, and external-access SQL return error."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('run_sql', {'query': query})

        assert res['ok'] is False
        assert 'error' in res
    finally:
        ingested.close()


def test_run_sql_strips_comments_and_validates_bad_args(
    tmp_path: pathlib.Path,
) -> None:
    """Comments in SELECT are ignored and bad limit/arg types return error."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        commented = reg.call(
            'run_sql',
            {
                'query': (
                    '-- lead comment\n'
                    'SELECT /* mid */ COUNT(*) AS cnt FROM metrics'
                )
            },
        )
        bad_limit = reg.call('run_sql', {'query': 'SELECT 1', 'limit': 'ten'})
        bad_samples = reg.call(
            'describe_table', {'table': 'metrics', 'sample_rows': True}
        )

        assert commented['ok'] is True
        assert bad_limit['ok'] is False
        assert bad_samples['ok'] is False
    finally:
        ingested.close()


def test_run_sql_enforces_query_timeout(tmp_path: pathlib.Path) -> None:
    """Queries exceeding query_timeout_s are interrupted and return error."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text('time,v\n1700000000,1\n', encoding='utf-8')
    cfg = ingest_schema.IngestConfig(
        limits=ingest_schema.IngestLimits(query_timeout_s=0.05)
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db', config=cfg)
    try:
        reg = tools.create_default_registry(ingested)
        heavy = (
            'SELECT COUNT(*) FROM range(50000000) AS a, range(50000000) AS b'
        )

        res = reg.call('run_sql', {'query': heavy})

        assert res['ok'] is False
        assert conftest.as_dict(res['error'])['code'] == 'query_timeout'
    finally:
        ingested.close()


def test_registry_returns_error_for_unknown_tool_and_bad_args(
    tmp_path: pathlib.Path,
) -> None:
    """Registry never raises on unknown tools, missing tables, or exceptions."""
    ingested = _make_sample_db(tmp_path)
    try:
        reg = tools.create_default_registry(ingested)

        unknown_tool = reg.call('nonexistent_tool', {})
        missing_table = reg.call('describe_table', {'table': 'missing'})
        bad_sql = reg.call('run_sql', {'query': 'SELECT nope FROM metrics'})

        assert unknown_tool['ok'] is False
        assert 'error' in unknown_tool
        assert missing_table['ok'] is False
        assert (
            conftest.as_dict(missing_table['error'])['code'] == 'unknown_table'
        )
        assert bad_sql['ok'] is False
        assert conftest.as_dict(bad_sql['error'])['code'] == 'sql_error'
        assert tables.validate_readonly_sql('SELECT 1;') == 'SELECT 1'
    finally:
        ingested.close()
