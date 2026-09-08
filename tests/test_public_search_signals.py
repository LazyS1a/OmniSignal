from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from omnisignal.connectors import FileRawResponseArchive
from omnisignal.connectors.public_search_policy import PublicSearchPolicy
from omnisignal.connectors.public_search_protocol import OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION
from omnisignal.connectors.public_search_signals import PublicSearchSignalsConnector
from omnisignal.contracts import CollectRequest, ConnectorFailure, ConnectorSpec, ErrorCategory
from omnisignal.governance import SourceApproval


ROOT = Path(__file__).parents[1]
FIXED_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def load_spec() -> ConnectorSpec:
    document = yaml.safe_load((ROOT / "examples/connectors/public_search_signals.yaml").read_text(encoding="utf-8"))
    return ConnectorSpec.model_validate(document)


def load_policy() -> PublicSearchPolicy:
    document = yaml.safe_load((ROOT / "examples/policies/public_search_signals.yaml").read_text(encoding="utf-8"))
    return PublicSearchPolicy.model_validate(document)


def approval() -> SourceApproval:
    return SourceApproval(
        source_id="public_search_signals",
        permission="registry:public_search_signals:test",
        authorization_basis="anonymous public responses",
        rate_budget_rpm=6,
    )


def signal_record(metric_type: str = "relative_interest") -> dict[str, object]:
    suggestion = metric_type == "suggestion_rank"
    return {
        "source_record_id": "suggestion-1" if suggestion else "trend-1",
        "query": "ChatGPT",
        "related_query": "chatgpt tutorial" if suggestion else None,
        "platform": "youtube",
        "metric_type": metric_type,
        "value": 1 if suggestion else 42,
        "unit": "rank_1_best" if suggestion else "index_0_100",
        "geo": "" if suggestion else "US",
        "time_window": "daily_snapshot" if suggestion else "today 3-m",
        "observed_at": "2026-09-04" if suggestion else "2026-09-04T00:00:00",
        "is_partial": False,
        "scope": "public_autocomplete_snapshot" if suggestion else "google_trends_same_request_scale",
        "comparison_group": None if suggestion else "comparison-1",
        "source_url": (
            "https://suggestqueries.google.com/complete/search"
            if suggestion
            else "https://trends.google.com/trends/"
        ),
        "collector_version": "fixture/1",
        "quality_status": "accepted",
    }


def worker_result(*records: dict[str, object], warnings: list[str] | None = None) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
        "status": "ok",
        "records": list(records),
        "warnings": warnings or [],
        "fetched_at": FIXED_NOW.isoformat(),
        "error": None,
    }


def test_policy_caps_comparison_at_five_unique_keywords() -> None:
    policy = load_policy()
    assert policy.keywords == ("ChatGPT", "Claude")
    with pytest.raises(ValueError):
        PublicSearchPolicy(keywords=("a", "b", "c", "d", "e", "f"))
    with pytest.raises(ValueError):
        PublicSearchPolicy(keywords=("ChatGPT", "chatgpt"))


@pytest.mark.parametrize("field,value", [("unit", "searches"), ("platform", "x"), ("geo", "CN"), ("query", "unknown"), ("value", True)])
def test_mislabeled_metrics_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    connector = PublicSearchSignalsConnector(load_spec(), load_policy(), approval(), workspace=tmp_path)
    record = signal_record()
    record[field] = value
    with pytest.raises(ConnectorFailure):
        connector._validate_record(record)


def test_worker_degrades_and_never_turns_failure_into_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnisignal.connectors import public_search_worker as worker
    def unavailable(job: object) -> list:
        raise RuntimeError("upstream unavailable")
    monkeypatch.setattr(worker, "_trend_records", unavailable)
    monkeypatch.setattr(worker, "_suggestion_records", lambda job, now: [signal_record("suggestion_rank")])
    result = worker.collect({"include_suggestions": True, "limit": 100}, now=FIXED_NOW)
    assert result["status"] == "ok"
    assert result["warnings"] == ["trend_unavailable"]
    assert len(result["records"]) == 1
    assert result["records"][0]["metric_type"] == "suggestion_rank"


def test_repeated_snapshot_is_idempotent(tmp_path: Path) -> None:
    from omnisignal.connectors.durable import run_connector_durably
    from sqlalchemy.engine import URL
    async def scenario() -> None:
        async def fake_worker(**kwargs: object) -> dict[str, object]:
            return worker_result(signal_record(), signal_record("suggestion_rank"))
        async def run():
            connector = PublicSearchSignalsConnector(load_spec(), load_policy(), approval(), workspace=tmp_path,
                                                    worker_runner=fake_worker)
            return await run_connector_durably(connector, database_url=URL.create("sqlite+pysqlite", database=str(tmp_path / "test.db")).render_as_string(),
                                              database_target="test", initialize_schema=True)
        first, second = await run(), await run()
        assert first.inserted == 2
        assert second.inserted == 0
        assert second.unchanged == 2
    asyncio.run(scenario())


def test_disabled_source_never_starts_worker(tmp_path: Path) -> None:
    from omnisignal.connectors.durable import run_connector_durably
    from omnisignal.storage import Base, SourceControlState, create_database_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.engine import URL
    url = URL.create("sqlite+pysqlite", database=str(tmp_path / "disabled.db")).render_as_string()
    engine = create_database_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(SourceControlState(source_id="public_search_signals", enabled=False, version=1,
                                       updated_by="test", reason="test"))
        session.commit()
    engine.dispose()
    async def scenario() -> None:
        async def forbidden_worker(**kwargs: object) -> dict:
            pytest.fail("disabled source invoked worker")
        connector = PublicSearchSignalsConnector(load_spec(), load_policy(), approval(), workspace=tmp_path,
                                                worker_runner=forbidden_worker)
        with pytest.raises(ConnectorFailure):
            await run_connector_durably(connector, database_url=url, database_target="test", initialize_schema=False)
    asyncio.run(scenario())


def test_empty_sources_are_marked_and_not_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnisignal.connectors import public_search_worker as worker
    monkeypatch.setattr(worker, "_trend_records", lambda job: [])
    monkeypatch.setattr(worker, "_suggestion_records", lambda job, now: [])
    result = worker.collect({"include_suggestions": True, "limit": 100}, now=FIXED_NOW)
    assert result["records"] == []
    assert set(result["warnings"]) == {"trend_empty", "suggestions_empty"}


def test_connector_preserves_metric_semantics_and_marks_partial_degradation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def fake_worker(**kwargs: object) -> dict[str, object]:
            assert kwargs["document"]["search_property"] == "youtube"  # type: ignore[index]
            return worker_result(signal_record(), signal_record("suggestion_rank"), warnings=["suggestions_partial"])

        connector = PublicSearchSignalsConnector(
            load_spec(),
            load_policy(),
            approval(),
            workspace=tmp_path / "worker",
            archive=FileRawResponseArchive(tmp_path / "archive"),
            worker_runner=fake_worker,
            now=lambda: FIXED_NOW,
        )
        batch = await connector.collect(CollectRequest(run_id="run-public-signals", limit=20))
        health = await connector.health()
        assert [record.payload["metric_type"] for record in batch.records] == [
            "relative_interest",
            "suggestion_rank",
        ]
        assert batch.records[0].payload["unit"] == "index_0_100"
        assert batch.records[1].payload["unit"] == "rank_1_best"
        assert all(record.raw_archive_sha256 for record in batch.records)
        assert batch.next_checkpoint.value["cycle_complete"] is True
        assert health.status.value == "degraded"
        assert health.detail_code == "suggestions_partial"
        await connector.close()

    asyncio.run(scenario())


def test_connector_rejects_absolute_search_volume_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        bad = signal_record()
        bad["metric_type"] = "search_volume"

        async def fake_worker(**kwargs: object) -> dict[str, object]:
            return worker_result(bad)

        connector = PublicSearchSignalsConnector(
            load_spec(), load_policy(), approval(), workspace=tmp_path, worker_runner=fake_worker
        )
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id="run-false-volume", limit=20))
        assert raised.value.category == ErrorCategory.SCHEMA_DRIFT

    asyncio.run(scenario())


def test_rate_limit_envelope_pauses_without_retrying(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = 0

        async def fake_worker(**kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "protocol_version": PROTOCOL_VERSION,
                "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
                "status": "error",
                "records": [],
                "warnings": ["trend_unavailable"],
                "fetched_at": FIXED_NOW.isoformat(),
                "error": {"kind": "rate_limit", "status": 429},
            }

        connector = PublicSearchSignalsConnector(
            load_spec(), load_policy(), approval(), workspace=tmp_path, worker_runner=fake_worker
        )
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id="run-rate-limit", limit=20))
        assert raised.value.category == ErrorCategory.RATE_LIMIT
        assert raised.value.retry_after_seconds == 3600
        assert calls == 1

    asyncio.run(scenario())
