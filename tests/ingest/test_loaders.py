"""Unit and integration tests for folder ingest, limits, and read-only DB."""

import collections
import decimal
import json
import os
import pathlib

import duckdb
import pytest

from incidents_agent import ingest
from incidents_agent.ingest import schema
from incidents_agent.llm import client
from incidents_agent.llm import config
from incidents_agent.llm import contracts
from incidents_agent.llm import ledger


class _ClassifierTransport:
    """Offline fake transport returning queued provider results or failures."""

    def __init__(
        self,
        outputs: list[contracts.ProviderResult | client.TransportFailure],
    ) -> None:
        self.outputs = collections.deque(outputs)
        self.calls = 0

    def input_bound(
        self, spec: config.ModelSpec, request: contracts.ChatRequest
    ) -> int | None:
        del spec, request
        return None

    def complete(
        self,
        spec: config.ModelSpec,
        request: contracts.ChatRequest,
        *,
        api_key: str,
        cache: bool,
        timeout: float,
    ) -> contracts.ProviderResult:
        del spec, request, api_key, cache, timeout
        self.calls += 1
        outcome = self.outputs.popleft()
        if isinstance(outcome, client.TransportFailure):
            raise outcome
        return outcome


def _make_guarded_classifier(
    tmp_path: pathlib.Path,
    *,
    budget_yaml: str,
    transport: client.Transport,
) -> tuple[client.GuardedClient, ledger.SpendLedger]:
    budget_path = tmp_path / 'budget.yaml'
    budget_path.write_text(budget_yaml, encoding='utf-8')
    spend = ledger.SpendLedger(tmp_path / 'spend.sqlite', budget_path)
    spec = config.ModelSpec('small', 'anthropic/fake-20250101', 'anthropic')
    models = config.Models({'small': spec}, 'small')
    rate_card: dict[str, object] = {
        'fake-20250101': {
            'litellm_provider': 'anthropic',
            'max_input_tokens': 20_000,
            'max_output_tokens': 1_000,
            'input_cost_per_token': '0.000001',
            'output_cost_per_token': '0.000002',
        }
    }
    guarded = client.GuardedClient(
        models,
        {'ANTHROPIC_API_KEY': 'fake-key'},
        spend,
        transport,
        catalog=rate_card,
        sleep=lambda _: None,
    )
    return (guarded, spend)


def _re2_ob_case_path() -> pathlib.Path | None:
    env_override = os.environ.get('INCIDENTS_RE2_OB_CASE')
    if env_override:
        candidate = pathlib.Path(env_override)
        if candidate.is_dir():
            return candidate
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    local = repo_root / 'agent_tasks' / 're2-ob' / 'cases' / 're2-ob-0001'
    return local if local.is_dir() else None


def test_ingest_loads_csv_parquet_jsonl_logs_and_documents(
    tmp_path: pathlib.Path,
) -> None:
    """Folder ingest loads all supported tabular, log, and document formats."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text(
        'time,api_cpu\n1700000000,10.5\n1700000060,88.2\n',
        encoding='utf-8',
    )
    (src / 'events.jsonl').write_text(
        '{"timestamp": "2024-01-01T00:00:00Z", "svc": "api"}\n',
        encoding='utf-8',
    )
    (src / 'app.log').write_text(
        '2024-01-01T00:00:01Z ERROR api Timeout\n'
        '2024-01-01T00:00:02Z INFO api Recovered\n',
        encoding='utf-8',
    )
    (src / 'runbook.md').write_text(
        '# API Runbook\n\n## Timeouts\n\nCheck pool limits.\n',
        encoding='utf-8',
    )
    pq_path = src / 'spans.parquet'
    writer = duckdb.connect(':memory:')
    writer.execute(
        f'COPY (SELECT 1700000000 AS time, 42.0 AS dur) '
        f"TO '{pq_path}' (FORMAT PARQUET)"
    )
    writer.close()

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        table_names = {entry.name for entry in result.catalog.tables}
        doc_ids = {doc.doc_id for doc in result.catalog.documents}
        chunk_rows = result.connection.execute(
            'SELECT doc_id, chunk_id FROM document_chunks'
        ).fetchall()

        metrics = result.catalog.get_table('metrics')
        app_tbl = result.catalog.get_table('app')

        assert table_names == {'metrics', 'events', 'app', 'spans'}
        assert doc_ids == {'runbook.md'}
        assert len(chunk_rows) == 1
        assert metrics is not None
        assert len(metrics.sample_rows) == 2
        assert app_tbl is not None
        assert app_tbl.time_info.time_usable is True
    finally:
        result.close()


def test_ingest_disambiguates_same_filename_in_different_folders(
    tmp_path: pathlib.Path,
) -> None:
    """Two metrics.csv files in different folders produce unique table names."""
    src = tmp_path / 'case'
    (src / 'zone_a').mkdir(parents=True)
    (src / 'zone_b').mkdir(parents=True)
    (src / 'metrics.csv').write_text('time,v\n1700000000,1\n', encoding='utf-8')
    (src / 'zone_a' / 'metrics.csv').write_text(
        'time,v\n1700000000,2\n', encoding='utf-8'
    )
    (src / 'zone_b' / 'metrics.csv').write_text(
        'time,v\n1700000000,3\n', encoding='utf-8'
    )

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        by_file = {
            entry.source_file: entry.name for entry in result.catalog.tables
        }

        assert by_file == {
            'metrics.csv': 'metrics',
            'zone_a/metrics.csv': 'zone_a__metrics',
            'zone_b/metrics.csv': 'zone_b__metrics',
        }
        assert len(set(by_file.values())) == 3
    finally:
        result.close()


def test_ingest_enforces_limits_and_continues_after_bad_files(
    tmp_path: pathlib.Path,
) -> None:
    """Oversized, corrupt, and over-count files are skipped without aborting."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'a_big.csv').write_text(
        'time,v\n1700000000,' + ('9' * 200) + '\n', encoding='utf-8'
    )
    (src / 'b_binary.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    (src / 'c_corrupt.jsonl').write_text('{invalid json', encoding='utf-8')
    (src / 'd_good1.csv').write_text('time,v\n1700000000,1\n', encoding='utf-8')
    (src / 'e_good2.csv').write_text('time,v\n1700000001,2\n', encoding='utf-8')
    (src / 'f_extra.csv').write_text('time,v\n1700000002,3\n', encoding='utf-8')

    cfg = schema.IngestConfig(
        limits=schema.IngestLimits(
            max_file_bytes=120,
            max_files=2,
            max_total_bytes=200,
        )
    )
    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        loaded = [t.source_file for t in result.catalog.tables]
        skipped = {
            s.relative_path: s.reason for s in result.catalog.skipped_files
        }

        assert loaded == ['d_good1.csv', 'e_good2.csv']
        assert 'max_file_bytes' in result.catalog.limits_hit
        assert 'max_files' in result.catalog.limits_hit
        assert 'a_big.csv' in skipped
        assert 'b_binary.png' in skipped
        assert 'c_corrupt.jsonl' in skipped
        assert 'f_extra.csv' in skipped
    finally:
        result.close()


def test_ingest_enforces_total_byte_limit(tmp_path: pathlib.Path) -> None:
    """Files exceeding remaining max_total_bytes are skipped and recorded."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'a.txt').write_text('first file\n' * 5, encoding='utf-8')
    (src / 'b.txt').write_text('second file\n' * 10, encoding='utf-8')

    cfg = schema.IngestConfig(
        limits=schema.IngestLimits(max_file_bytes=500, max_total_bytes=70)
    )
    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        assert [d.source_file for d in result.catalog.documents] == ['a.txt']
        assert 'max_total_bytes' in result.catalog.limits_hit
        assert any(
            s.relative_path == 'b.txt' for s in result.catalog.skipped_files
        )
    finally:
        result.close()


def test_ingest_handles_empty_table_without_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """Empty CSV with header and empty log file ingest as zero-row tables."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text('time,cpu\n', encoding='utf-8')
    (src / 'empty.log').write_text('', encoding='utf-8')

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        metrics = result.catalog.get_table('metrics')
        empty_log = result.catalog.get_table('empty')

        assert metrics is not None
        assert metrics.row_count == 0
        assert metrics.time_info.time_usable is False
        assert empty_log is not None
        assert empty_log.row_count == 0
    finally:
        result.close()


@pytest.mark.integration
def test_ingest_connection_is_read_only_and_disables_external_access(
    tmp_path: pathlib.Path,
) -> None:
    """Returned DuckDB connection blocks writes and external file reads."""
    src = tmp_path / 'case'
    src.mkdir()
    (src / 'metrics.csv').write_text(
        'time,cpu\n1700000000,1\n', encoding='utf-8'
    )
    secret_csv = tmp_path / 'outside.csv'
    secret_csv.write_text('secret\npassword123\n', encoding='utf-8')

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out')
    try:
        with pytest.raises(duckdb.Error):
            result.connection.execute('INSERT INTO metrics VALUES (1, 2, 1.0)')
        with pytest.raises(duckdb.Error):
            result.connection.execute(
                f"SELECT * FROM read_csv_auto('{secret_csv}')"
            ).fetchall()
    finally:
        result.close()


def test_ingest_missing_folder_raises_file_not_found(
    tmp_path: pathlib.Path,
) -> None:
    """Ingesting a non-existent folder fails with FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        ingest.ingest_folder(tmp_path / 'does_not_exist')


@pytest.mark.integration
def test_ingest_real_re2_ob_case(tmp_path: pathlib.Path) -> None:
    """Ingest one real exported RE2-OB case when available locally."""
    case_dir = _re2_ob_case_path()
    if case_dir is None:
        pytest.skip('RE2-OB blind export not present locally')

    result = ingest.ingest_folder(case_dir, workdir=tmp_path / 're2-db')
    try:
        metrics = result.catalog.get_table('metrics')
        logs = result.catalog.get_table('logs')
        traces = result.catalog.get_table('traces')

        assert result.catalog.skipped_files == ()
        assert metrics is not None
        assert metrics.time_info.column == 'time'
        assert metrics.time_info.unit == 'unix_s'
        assert metrics.time_info.time_usable is True
        assert logs is not None
        assert logs.time_info.column == 'timestamp'
        assert logs.time_info.unit == 'unix_ns'
        assert logs.time_info.time_usable is True
        assert traces is not None
        assert traces.time_info.column in ('startTimeMillis', 'startTime')
        assert traces.time_info.time_usable is True
        assert len(traces.relationships) == 1
        assert traces.relationships[0].kind == 'span_parent'
        assert traces.relationships[0].overlap_ratio == 1.0
    finally:
        result.close()


@pytest.mark.integration
def test_ingest_guarded_classifier_settles_spend_in_ledger(
    tmp_path: pathlib.Path,
) -> None:
    """Guarded fallback reserves, settles Decimal cost, and serialises."""
    src = tmp_path / 'incident_alpha'
    src.mkdir()
    (src / 'events.txt').write_text('evt|service|ok\n' * 30, encoding='utf-8')
    (src / 'metrics.csv').write_text(
        'time,cpu\n1700000000,1\n', encoding='utf-8'
    )
    transport = _ClassifierTransport(
        [
            contracts.ProviderResult(
                message=contracts.Message(
                    'assistant',
                    '{"kind": "log", "chunk_strategy": "none", '
                    '"reason": "delimited log lines"}',
                ),
                usage=contracts.Usage(input_tokens=300, output_tokens=20),
                served_model='fake-20250101',
                request_id='req-1',
            )
        ]
    )
    guarded, spend = _make_guarded_classifier(
        tmp_path,
        budget_yaml='total_usd: 1.00\nsafety_margin_usd: 0.10\n',
        transport=transport,
    )
    run = contracts.Run('ingest-run-1')
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=guarded,
        classifier_run=run,
    )

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        events_cls = next(
            cls
            for cls in result.catalog.classifications
            if cls.relative_path == 'events.txt'
        )
        counters = spend.counters(run)
        records = spend.attempt_records(run)
        serialised = json.dumps(result.catalog.to_dict())
        parsed_catalog = json.loads(serialised)
        events_entry = parsed_catalog['classifications'][0]

        assert transport.calls == 1
        assert result.catalog.get_table('events') is not None
        assert events_cls.kind == 'log'
        assert events_cls.used_model_fallback is True
        assert events_cls.model_cost_usd == decimal.Decimal('0.00034')
        assert counters.spent == decimal.Decimal('0.00034')
        assert counters.held == decimal.Decimal(0)
        assert records[0]['case_id'] == 'incident_alpha'
        assert events_entry['relative_path'] == 'events.txt'
        assert decimal.Decimal(events_entry['model_cost_usd']) == (
            decimal.Decimal('0.00034')
        )
    finally:
        result.close()


@pytest.mark.integration
def test_ingest_guarded_classifier_budget_refusal_halts_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """BudgetRefused degrades to rules and halts further fallback calls."""
    src = tmp_path / 'case_budget'
    src.mkdir()
    (src / 'a_note.txt').write_text(
        'First ambiguous prose note.\n' * 60, encoding='utf-8'
    )
    (src / 'b_note.txt').write_text(
        'Second ambiguous prose note.\n' * 60, encoding='utf-8'
    )
    transport = _ClassifierTransport([])
    guarded, spend = _make_guarded_classifier(
        tmp_path,
        budget_yaml=(
            'total_usd: 1.00\nsafety_margin_usd: 0.10\n'
            'per_case_cap_usd: 0.001\n'
        ),
        transport=transport,
    )
    run = contracts.Run('ingest-budget')
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=guarded,
        classifier_run=run,
    )

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        by_path = {
            cls.relative_path: cls for cls in result.catalog.classifications
        }

        assert transport.calls == 0
        assert spend.attempt_records(run) == []
        assert len(result.catalog.documents) == 2
        assert by_path['a_note.txt'].used_model_fallback is False
        assert by_path['a_note.txt'].fallback_halted is True
        assert 'BudgetRefused' in by_path['a_note.txt'].reason
        assert by_path['b_note.txt'].used_model_fallback is False
        assert 'model fallback unavailable' not in by_path['b_note.txt'].reason
    finally:
        result.close()


@pytest.mark.integration
def test_ingest_guarded_classifier_ambiguous_timeout_retains_hold(
    tmp_path: pathlib.Path,
) -> None:
    """BillingUncertain keeps the reservation held and stops further calls."""
    src = tmp_path / 'case_timeout'
    src.mkdir()
    (src / 'a_note.txt').write_text(
        'First ambiguous note for timeout.\n' * 60, encoding='utf-8'
    )
    (src / 'b_note.txt').write_text(
        'Second ambiguous note after halt.\n' * 60, encoding='utf-8'
    )
    transport = _ClassifierTransport(
        [client.TransportFailure('timed out', retryable=False)]
    )
    guarded, spend = _make_guarded_classifier(
        tmp_path,
        budget_yaml='total_usd: 1.00\nsafety_margin_usd: 0.10\n',
        transport=transport,
    )
    run = contracts.Run('ingest-timeout')
    cfg = schema.IngestConfig(
        enable_model_classification=True,
        classifier_client=guarded,
        classifier_run=run,
    )

    result = ingest.ingest_folder(src, workdir=tmp_path / 'out', config=cfg)
    try:
        by_path = {
            cls.relative_path: cls for cls in result.catalog.classifications
        }
        records = spend.attempt_records(run)

        assert transport.calls == 1
        assert len(result.catalog.documents) == 2
        assert by_path['a_note.txt'].used_model_fallback is False
        assert by_path['a_note.txt'].fallback_halted is True
        assert 'BillingUncertain' in by_path['a_note.txt'].reason
        assert 'model fallback unavailable' not in by_path['b_note.txt'].reason
        assert spend.snapshot().held > decimal.Decimal(0)
        assert records[0]['status'] == 'ambiguous'
    finally:
        result.close()
