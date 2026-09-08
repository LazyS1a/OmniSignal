from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from omnisignal.api import create_app
from omnisignal.collection_jobs import CollectionTaskCatalog
from omnisignal.config import Environment, Settings
from omnisignal.storage import Base, IngestedRecord, create_database_engine


ROOT = Path(__file__).parents[1]


class FakeExecutor:
    def reconcile_orphans(self) -> int:
        return 0

    def submit(self, job_id: str) -> None:
        raise AssertionError(f"read-only test submitted {job_id}")


def _record(source_id: str, identity: str, payload: dict[str, object], seen_at: datetime) -> IngestedRecord:
    return IngestedRecord(
        source_id=source_id,
        source_record_id=identity,
        payload=payload,
        raw_hash="b" * 64,
        schema_version="1.0",
        permission="public-web",
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )


def _youtube_snapshot(observed: datetime, context: dict[str, object]) -> dict[str, object]:
    return {
        "metric_type": "video_search_sample_visibility",
        "query": "AI note taking tools",
        "platform": "youtube",
        "scope": "yt_dlp_video_search_order",
        "requested_language": "en",
        "is_example": True,
        "requested_top_k": 2,
        "valid_result_count": 2,
        "quality_status": "complete",
        "fetched_at": observed.isoformat(),
        "collector_version": "fixture",
        "rule_hash": "c" * 64,
        "calculation_version": "1.0",
        "warnings": [],
        "brands": [
            {
                "brand_id": "example_owned",
                "name": "Notion (example)",
                "role": "owned",
                "count": 1,
                "denominator": 2,
                "sample_share_percent": 50.0,
                "first_position": 1,
                "official_count": 0,
                "third_party_title_count": 0,
                "unverified_origin_count": 1,
            },
            {
                "brand_id": "example_competitor",
                "name": "Obsidian (example)",
                "role": "competitor",
                "count": 0,
                "denominator": 2,
                "sample_share_percent": 0.0,
                "first_position": None,
                "official_count": 0,
                "third_party_title_count": 0,
                "unverified_origin_count": 0,
            },
        ],
        "evidence": [
            {
                "position": 1,
                "video_id": "aaaaaaaaaaa",
                "title": "Notion workflow",
                "matches": [{"brand_id": "example_owned", "aliases": ["notion"],
                             "origin": "unverified", "official_channel_match": False}],
            },
            {"position": 2, "video_id": "bbbbbbbbbbb", "title": "Other workflow", "matches": []},
        ],
        "observation_context": context,
    }


def test_trends_api_preserves_units_denominators_and_versioned_context(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'trends.db').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    catalog = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    public_context = catalog.observation_context(catalog.tasks["public_search_signals_example"]).model_dump(mode="json")
    youtube_context = catalog.observation_context(catalog.tasks["youtube_visibility_example"]).model_dump(mode="json")
    with Session(engine) as session:
        for index, value in enumerate((20, 35), start=1):
            observed = now - timedelta(days=3 - index)
            session.add(_record("public_search_signals", f"public-{index}", {
                "query": "ChatGPT",
                "related_query": None,
                "platform": "youtube",
                "metric_type": "relative_interest",
                "value": value,
                "unit": "index_0_100",
                "geo": "US",
                "time_window": "today 3-m",
                "observed_at": (observed.replace(tzinfo=None) if index == 1 else observed).isoformat(),
                "is_partial": False,
                "scope": "google_trends_same_request_scale",
                "comparison_group": "fixture-comparison",
                "source_url": "https://trends.google.com/trends/",
                "collector_version": "fixture",
                "quality_status": "accepted",
                "observation_context": public_context,
            }, observed))
        snapshot_time = now - timedelta(days=1)
        session.add(_record("youtube_visibility", "a" * 64,
                            {"visibility_snapshot": _youtube_snapshot(snapshot_time, youtube_context)}, snapshot_time))
        session.commit()

    app = create_app(
        settings=Settings(environment=Environment.TEST, database_url=database_url),
        engine=engine,
        registry_path=ROOT / "governance" / "source_registry.yaml",
        collection_tasks_path=ROOT / "config" / "collection_tasks.yaml",
        snapshot_schedules_path=ROOT / "config" / "snapshot_schedules.yaml",
        collection_executor=FakeExecutor(),
        control_principals=(),
    )

    async def requests():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get("/ops/trends", params={"days": 30}), await client.get("/ops/snapshot-schedules")

    trends, schedules = asyncio.run(requests())
    assert trends.status_code == schedules.status_code == 200
    document = trends.json()
    relative = next(item for item in document["items"] if item["metric_type"] == "relative_interest")
    share = next(item for item in document["items"] if item["metric_type"] == "video_sample_share"
                 and item["subject"] == "Notion (example)")
    assert relative["unit"] == "index_0_100" and relative["trend_ready"] is True
    assert relative["observation_context"]["keyword_set"] == {"id": "example_ai_assistants", "version": 1}
    assert share["points"][0]["numerator"] == 1 and share["points"][0]["denominator"] == 2
    assert document["semantics"]["absolute_search_volume_available"] is False
    assert document["semantics"]["cross_platform_global_share_available"] is False
    assert schedules.json()["auto_start"] is False
    assert all(item["status"] == "paused" and item["eligible_to_trigger"] is False
               for item in schedules.json()["items"])
    engine.dispose()


def _render_trend_fixture() -> None:
    from omnisignal.ops_ui.trends import render_trends

    def load(path, params=None):
        assert path == "/ops/trends"
        return {
            "window": {"from": "2026-09-01T00:00:00Z", "to": "2026-09-03T00:00:00Z"},
            "row_scan_truncated": False,
            "items": [{
                "series_id": "a" * 64,
                "source_id": "public_search_signals",
                "platform": "youtube",
                "query": "ChatGPT",
                "subject": None,
                "metric_type": "relative_interest",
                "unit": "index_0_100",
                "scope": "google_trends_same_request_scale",
                "sample_count": 2,
                "distinct_days": 2,
                "trend_ready": True,
                "observation_context": {
                    "keyword_set": {"id": "example_ai_assistants", "version": 1},
                    "entity_set": None,
                    "access_tier": "anonymous_public",
                    "scope": "fixture scope",
                    "definition_hash": "b" * 64,
                    "provenance_status": "versioned",
                },
                "coverage": {"geo": "US", "absolute_search_volume": False},
                "points": [
                    {"observed_at": "2026-09-01T00:00:00Z", "value": 20, "numerator": None,
                     "denominator": None, "quality_status": "accepted", "is_partial": False},
                    {"observed_at": "2026-09-02T00:00:00Z", "value": 30, "numerator": None,
                     "denominator": None, "quality_status": "accepted", "is_partial": False},
                ],
            }],
        }

    render_trends(load)


def test_trend_ui_renders_stored_series_without_claiming_absolute_volume() -> None:
    app = AppTest.from_function(_render_trend_fixture).run(timeout=10)
    assert not app.exception
    assert any(metric.label == "可画趋势" and metric.value == "是" for metric in app.metric)
    assert app.dataframe
    assert any("没有全网真实搜索次数" in caption.value for caption in app.caption)
