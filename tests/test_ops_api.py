from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnisignal.api import create_app
from omnisignal.config import Environment, Settings
from omnisignal.storage import (
    AuditEvent,
    Base,
    ConnectorState,
    IngestedRecord,
    IngestionRun,
    NormalizationRun,
    NormalizationRunMembership,
    NormalizedRecord,
    PluginOutput,
    PluginRun,
    QualityEvent,
    RunStatus,
    create_database_engine,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _seed(engine) -> str:
    normalized_id = "a" * 64
    with Session(engine) as session:
        session.add_all(
            [
                ConnectorState(
                    source_id="fixture_source",
                    checkpoint={"cursor": "must-not-leak"},
                    checkpoint_version=2,
                    updated_at=NOW,
                ),
                IngestionRun(
                    run_id="11111111-1111-1111-1111-111111111111",
                    source_id="fixture_source",
                    idempotency_key="ops-test",
                    status=RunStatus.SUCCEEDED,
                    records_seen=1,
                    records_written=1,
                    started_at=NOW,
                    finished_at=NOW,
                    created_at=NOW,
                ),
                IngestedRecord(
                    source_id="fixture_source",
                    source_record_id="record-1",
                    payload={"title": "OmniSignal", "text": "searchable body"},
                    raw_hash="b" * 64,
                    schema_version="1.0",
                    permission="fixture/v1",
                    raw_archive_sha256="c" * 64,
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                ),
                NormalizationRun(
                    run_id="d" * 64,
                    config_version="1.0",
                    config_hash="e" * 64,
                    normalizer_version="1.0.0",
                    input_set_hash="f" * 64,
                    status="succeeded",
                    input_count=1,
                    normalized_count=1,
                    unconfigured_count=0,
                    warning_count=1,
                    quarantined_count=0,
                    duplicate_group_count=0,
                    created_at=NOW,
                    finished_at=NOW,
                ),
                NormalizedRecord(
                    normalized_id=normalized_id,
                    source_id="fixture_source",
                    source_record_id="record-1",
                    input_raw_hash="b" * 64,
                    raw_archive_sha256="c" * 64,
                    permission="fixture/v1",
                    source_schema_version="1.0",
                    normalizer_version="1.0.0",
                    config_hash="e" * 64,
                    normalized_hash="1" * 64,
                    canonical_url="https://example.test/record-1",
                    title="OmniSignal",
                    text="searchable body",
                    language="en",
                    entity_ids=["omnisignal"],
                    quality_status="warning",
                    quality_codes=["language_undetermined"],
                    created_at=NOW,
                ),
                NormalizationRunMembership(
                    normalization_run_id="d" * 64,
                    source_id="fixture_source",
                    source_record_id="record-1",
                    input_raw_hash="b" * 64,
                    normalized_id=normalized_id,
                    disposition="normalized_new",
                    created_at=NOW,
                ),
                QualityEvent(
                    event_id="2" * 64,
                    normalization_run_id="d" * 64,
                    normalized_id=normalized_id,
                    code="language_undetermined",
                    severity="warning",
                    field="language",
                    detail={},
                    created_at=NOW,
                ),
                PluginRun(
                    run_id="33333333-3333-3333-3333-333333333333",
                    execution_id="3" * 64,
                    plugin_id="text_stats",
                    plugin_version="1.0.0",
                    manifest_hash="4" * 64,
                    output_schema_version="1.0",
                    status="succeeded",
                    records_seen=1,
                    records_sent=1,
                    records_skipped=0,
                    output_count=1,
                    output_hash="5" * 64,
                    started_at=NOW,
                    finished_at=NOW,
                    created_at=NOW,
                ),
                PluginOutput(
                    run_id="33333333-3333-3333-3333-333333333333",
                    normalized_id=normalized_id,
                    values={"text_chars": 15},
                    quality_status="accepted",
                    quality_codes=[],
                    created_at=NOW,
                ),
                AuditEvent(
                    actor="fixture",
                    action="ops_test",
                    target="fixture_source",
                    detail={
                        "api_token": "must-not-leak",
                        "workspace": "D:\\private\\worker",
                        "safe": "visible",
                    },
                    created_at=NOW,
                ),
            ]
        )
        session.commit()
    return normalized_id


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        """registry_version: 1
updated_at: '2026-09-03'
sources:
  - id: fixture_source
    display_name: Fixture Source
    connector_class: api
    status: allowed
    authorization_basis: project fixture
    rate_budget_rpm: 30
    kill_switch: true
""",
        encoding="utf-8",
    )
    return path


def test_failed_ingestion_counts_and_lowercase_filter_match_enum_storage(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'failures.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for run_status in (RunStatus.FAILED, RunStatus.PAUSED, RunStatus.QUARANTINED, RunStatus.SUCCEEDED):
            session.add(IngestionRun(source_id="fixture_source", idempotency_key=run_status.value, status=run_status))
        session.commit()
    app = create_app(settings=Settings(environment=Environment.TEST, database_url="sqlite://"), engine=engine)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                summary = await client.get("/ops/summary")
                failed = await client.get("/ops/runs", params={"kind": "ingestion", "status": "failed"})
                return summary, failed

    summary, failed = asyncio.run(scenario())
    assert summary.json()["ingestion_runs"] == {"total": 4, "stopped": 3}
    assert len(failed.json()["items"]) == 1
    assert failed.json()["items"][0]["status"] == "failed"


def test_unfinished_review_includes_old_runs_outside_summary_window(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'unfinished.db').as_posix()}")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        for index, (run_status, hours_ago) in enumerate(((RunStatus.RUNNING, 72), (RunStatus.PENDING, 48), (RunStatus.RUNNING, 0), (RunStatus.SUCCEEDED, 72), (RunStatus.FAILED, 72))):
            session.add(IngestionRun(source_id="fixture_source", idempotency_key=str(index), status=run_status, created_at=now - timedelta(hours=hours_ago)))
        session.commit()
    app = create_app(settings=Settings(environment=Environment.TEST, database_url="sqlite://"), engine=engine)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.get("/ops/summary", params={"window_hours": 1})

    result = asyncio.run(scenario()).json()
    assert result["ingestion_runs"] == {"total": 1, "stopped": 0}
    assert result["unfinished_ingestion"] == {"total": 3, "needs_review": 2, "review_after_hours": 24, "scope": "all_history"}
    with Session(engine) as session:
        assert sum(run.status == RunStatus.RUNNING for run in session.scalars(select(IngestionRun))) == 2
    engine.dispose()


def test_ops_endpoints_are_bounded_traceable_and_redacted(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'ops.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    normalized_id = _seed(engine)
    settings = Settings(environment=Environment.TEST, database_url="sqlite+pysqlite:///:memory:")
    app = create_app(settings=settings, engine=engine, registry_path=_registry(tmp_path))

    async def scenario() -> dict[str, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return {
                    "summary": await client.get("/ops/summary"),
                    "sources": await client.get("/ops/sources"),
                    "runs": await client.get("/ops/runs", params={"kind": "plugin", "limit": 1}),
                    "records": await client.get(
                        "/ops/records", params={"source_id": "fixture_source", "q": "searchable", "limit": 1}
                    ),
                    "detail": await client.get(f"/ops/records/{normalized_id}"),
                    "quality": await client.get("/ops/quality"),
                    "audit": await client.get("/ops/audit"),
                    "bad_limit": await client.get("/ops/records", params={"limit": 1000}),
                }

    responses = asyncio.run(scenario())
    assert all(response.status_code == 200 for key, response in responses.items() if key != "bad_limit")
    assert responses["bad_limit"].status_code == 422

    coverage = responses["summary"].json()["archive_coverage"]
    assert coverage == {
        "numerator": 1,
        "denominator": 1,
        "ratio": 1.0,
        "scope": "active_ingested_records",
    }
    source = responses["sources"].json()["items"][0]
    assert source["source_id"] == "fixture_source"
    assert source["checkpoint_version"] == 2
    assert "checkpoint" not in source
    assert source["latest_run"]["status"] == "succeeded"
    assert responses["runs"].json()["items"][0]["kind"] == "plugin"
    assert responses["records"].json()["total"] == 1
    assert responses["detail"].json()["quality_events"][0]["code"] == "language_undetermined"
    assert responses["detail"].json()["plugin_outputs"][0]["values"] == {"text_chars": 15}
    quality = responses["quality"].json()["quality_statuses"][0]
    assert {"numerator", "denominator", "ratio", "scope"}.issubset(quality)
    audit_text = responses["audit"].text
    assert "must-not-leak" not in audit_text
    assert "D:\\\\private" not in audit_text
    assert "visible" in audit_text


def test_records_csv_export_is_bounded_traceable_and_formula_safe(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'export.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    normalized_id = _seed(engine)
    with Session(engine) as session:
        record = session.get(NormalizedRecord, normalized_id)
        assert record is not None
        record.canonical_url = "https://example.test/" + ("x" * 3000)
        record.title = "=2+2"
        record.text = "  @SUM(A1:A2)" + ("x" * 5000)
        session.commit()
    settings = Settings(environment=Environment.TEST, database_url="sqlite+pysqlite:///:memory:")
    app = create_app(settings=settings, engine=engine, registry_path=_registry(tmp_path))

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                exported = await client.get(
                    "/ops/exports/records.csv",
                    params={"source_id": "fixture_source", "quality_status": "warning", "limit": 1},
                )
                empty = await client.get(
                    "/ops/exports/records.csv", params={"source_id": "missing", "limit": 1}
                )
                bad_limit = await client.get("/ops/exports/records.csv", params={"limit": 1001})
                return exported, empty, bad_limit

    exported, empty, bad_limit = asyncio.run(scenario())
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert exported.headers["content-disposition"] == 'attachment; filename="omnisignal-records.csv"'
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.headers["x-omnisignal-export-rows"] == "1"
    assert exported.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    row = rows[0]
    assert row["source_id"] == "fixture_source"
    assert row["source_record_id"] == "record-1"
    assert row["source_schema_version"] == "1.0"
    assert row["normalizer_version"] == "1.0.0"
    assert row["config_hash"] == "e" * 64
    assert row["raw_archive_sha256"] == "c" * 64
    assert len(row["canonical_url"]) == 2048
    assert row["canonical_url_truncated"] == "true"
    assert row["title"] == "'=2+2"
    assert row["text"].startswith("'  @SUM")
    assert len(row["text"]) == 4001
    assert row["title_truncated"] == "false"
    assert row["text_truncated"] == "true"

    empty_rows = list(csv.DictReader(io.StringIO(empty.content.decode("utf-8-sig"))))
    assert empty.status_code == 200
    assert empty.headers["x-omnisignal-export-rows"] == "0"
    assert empty_rows == []
    assert bad_limit.status_code == 422


def test_records_csv_export_reports_database_unavailable(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'missing-schema.sqlite3').as_posix()}"
    engine = create_database_engine(database_url)
    settings = Settings(environment=Environment.TEST, database_url=database_url)
    app = create_app(settings=settings, engine=engine, registry_path=_registry(tmp_path))

    response = asyncio.run(_request_export(app))

    assert response.status_code == 503
    assert response.json() == {"detail": "operations database unavailable"}
    assert "sqlite" not in response.text.lower()
    engine.dispose()


async def _request_export(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ops/exports/records.csv")
