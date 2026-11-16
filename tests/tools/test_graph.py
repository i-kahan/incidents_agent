"""Unit tests for dependency_graph over relationship tables and cycles."""

import pathlib

from incidents_agent import ingest
from incidents_agent import tools
from tests.tools import conftest


def test_dependency_graph_handles_caller_callee_and_cycles(
    tmp_path: pathlib.Path,
) -> None:
    """Caller/callee tables with cycles aggregate before/after metrics."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'calls.csv').write_text(
        'time,caller,callee,duration_ms,status\n'
        '1700000010,api,orders,10.0,ok\n'
        '1700000020,orders,cache,2.0,ok\n'
        '1700000030,cache,orders,3.0,ok\n'
        '1700000210,api,orders,150.0,error\n'
        '1700000220,orders,cache,90.0,error\n'
        '1700000230,cache,orders,85.0,error\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        full = reg.call('dependency_graph', {'split_time': 1_700_000_100})
        orders_hood = reg.call(
            'dependency_graph',
            {'split_time': 1_700_000_100, 'component': 'cache'},
        )

        assert full['ok'] is True
        assert full['has_relationships'] is True
        edges = conftest.as_dict_list(conftest.untrusted_content(full)['edges'])
        pairs = {(str(e['caller']), str(e['callee'])): e for e in edges}
        assert ('orders', 'cache') in pairs
        assert ('cache', 'orders') in pairs
        assert pairs[('api', 'orders')]['error_rate_before'] == 0.0
        assert pairs[('api', 'orders')]['error_rate_after'] == 1.0
        hood_edges = conftest.as_dict_list(
            conftest.untrusted_content(orders_hood)['edges']
        )
        assert len(hood_edges) == 2
    finally:
        ingested.close()


def test_dependency_graph_joins_span_parent_and_normalises_service_alias(
    tmp_path: pathlib.Path,
) -> None:
    """Span/parentSpanID trace tables build service edges and match aliases."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'traces.csv').write_text(
        'startTimeMillis,spanID,parentSpanID,serviceName,duration,statusCode\n'
        '1700000010000,s1,,frontendservice,20,0\n'
        '1700000010000,s2,s1,checkoutservice,15,0\n'
        '1700000210000,s3,,frontendservice,500,0\n'
        '1700000210000,s4,s3,checkoutservice,480,14\n',
        encoding='utf-8',
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call(
            'dependency_graph',
            {'split_time': 1_700_000_100, 'component': 'frontend'},
        )

        assert res['ok'] is True
        edges = conftest.as_dict_list(conftest.untrusted_content(res)['edges'])
        assert len(edges) == 1
        assert edges[0]['caller'] == 'frontendservice'
        assert edges[0]['callee'] == 'checkoutservice'
        assert edges[0]['error_rate_after'] == 1.0
        assert edges[0]['mean_latency_after'] == 480.0
    finally:
        ingested.close()


def test_dependency_graph_states_explicitly_when_no_relationships_exist(
    tmp_path: pathlib.Path,
) -> None:
    """Tables without relationships explicitly report lack of direction data."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text(
        'time,cpu\n1700000000,10\n1700000010,90\n', encoding='utf-8'
    )
    ingested = ingest.ingest_folder(src, workdir=tmp_path / 'db')
    try:
        reg = tools.create_default_registry(ingested)

        res = reg.call('dependency_graph', {})

        assert res['ok'] is True
        assert res['has_relationships'] is False
        assert conftest.untrusted_content(res)['edges'] == []
        assert 'cannot be established' in str(res['message'])
    finally:
        ingested.close()
