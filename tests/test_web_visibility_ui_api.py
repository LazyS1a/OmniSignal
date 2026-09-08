from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from omnisignal.api import create_app
from omnisignal.collection_jobs import CollectionTaskCatalog
from omnisignal.config import Environment, Settings
from omnisignal.measurement import VersionRef
from omnisignal.storage import Base, IngestedRecord, create_database_engine


ROOT = Path(__file__).parents[1]
ARCHIVE = "c" * 64


def test_profile_snapshot_exposes_missing_engine(tmp_path):
    from types import SimpleNamespace
    from omnisignal.api.web_visibility import project_snapshot
    from omnisignal.search_profiles import SearchProfile
    engine, database_url = _seeded(tmp_path)
    catalog = CollectionTaskCatalog(ROOT / "config/collection_tasks.yaml", ROOT)
    profile = SearchProfile(queries=["ChatGPT"], engines=["duckduckgo", "brave"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        collection_catalog=catalog,
        search_profiles=SimpleNamespace(profiles={"example_ai_assistants": profile}))))
    from sqlalchemy import select
    with Session(engine) as session:
        result = project_snapshot(list(session.scalars(select(IngestedRecord))), request, detail=True)
    assert result["quality_status"] == "incomplete"
    assert result["missing_slices"] == [{"query": "ChatGPT", "engine": "brave"}]
    assert result["slices"][0]["quality_status"] == "complete"


def _payload(*, position: int, title: str, url: str, entity_context: bool = False) -> dict[str, object]:
    catalog = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    task = catalog.tasks["searxng_results_example"]
    entity_ref = VersionRef(id="example_note_tools", version=1) if entity_context else None
    context = catalog.measurements.context(
        keyword_ref=task.keyword_set,
        entity_ref=entity_ref,
        access_tier=task.access_tier,
        scope=task.observation_scope,
    )
    return {
        "category": "general",
        "collector_version": "searxng-http-json/1",
        "engine": "duckduckgo",
        "language": "en-US",
        "observed_at": "2026-09-07T00:00:00Z",
        "position": position,
        "published_at": None,
        "quality_status": "accepted",
        "query": "ChatGPT",
        "sample_complete": True,
        "scope": "searxng_engine_result_snapshot",
        "source_url": "http://127.0.0.1:8888/search",
        "text": title,
        "time_range": None,
        "title": title,
        "url": url,
        "observation_context": context.model_dump(mode="json"),
    }


def _seeded(tmp_path: Path, *, entity_context: bool = False, unsafe: bool = False):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'web-visibility.db').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    first_url = "javascript:alert(1)" if unsafe else "https://www.notion.so/product"
    rows = [
        IngestedRecord(
            source_id="searxng_results",
            source_record_id="a" * 64,
            payload=_payload(position=1, title="Notion product", url=first_url, entity_context=entity_context),
            raw_hash="1" * 64,
            schema_version="1.0",
            permission="fixture",
            raw_archive_sha256=ARCHIVE,
        ),
        IngestedRecord(
            source_id="searxng_results",
            source_record_id="b" * 64,
            payload=_payload(
                position=2,
                title="Obsidian product",
                url="https://obsidian.md/",
                entity_context=entity_context,
            ),
            raw_hash="2" * 64,
            schema_version="1.0",
            permission="fixture",
            raw_archive_sha256=ARCHIVE,
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
    return engine, database_url


async def _requests(engine, database_url: str, paths: list[str]):
    app = create_app(settings=Settings(environment=Environment.TEST, database_url=database_url), engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return [await client.get(path) for path in paths]


def test_web_visibility_api_projects_snapshot_slices_and_optional_entity_counts(tmp_path: Path) -> None:
    engine, database_url = _seeded(tmp_path, entity_context=True)
    listing, detail = asyncio.run(
        _requests(engine, database_url, ["/ops/web-visibility", f"/ops/web-visibility/{ARCHIVE}"])
    )

    assert listing.status_code == detail.status_code == 200
    item = listing.json()["items"][0]
    assert item["result_count"] == 2 and item["query_count"] == 1
    assert item["engines"] == ["duckduckgo"]
    slice_item = detail.json()["slices"][0]
    assert slice_item["query"] == "ChatGPT" and slice_item["denominator"] == 2
    counts = {entity["entity_id"]: entity for entity in slice_item["entities"]}
    assert counts["example_owned"]["sample_share_percent"] == 50
    assert counts["example_competitor"]["sample_share_percent"] == 50
    assert [result["position"] for result in slice_item["results"]] == [1, 2]
    engine.dispose()


def test_web_visibility_api_hides_invalid_snapshot_and_bounds_queries(tmp_path: Path) -> None:
    engine, database_url = _seeded(tmp_path, unsafe=True)
    listing, detail, bad_limit, absent = asyncio.run(
        _requests(
            engine,
            database_url,
            [
                "/ops/web-visibility",
                f"/ops/web-visibility/{ARCHIVE}",
                "/ops/web-visibility?limit=51",
                "/ops/web-visibility?q=absent",
            ],
        )
    )

    assert listing.json()["items"][0]["validation_status"] == "invalid_snapshot"
    assert detail.status_code == 409 and "javascript:" not in detail.text
    assert bad_limit.status_code == 422
    assert absent.json()["total"] == 0
    engine.dispose()


def test_web_visibility_query_filter_selects_snapshots_without_truncating_them(tmp_path: Path) -> None:
    engine, database_url = _seeded(tmp_path)
    with Session(engine) as session:
        second = session.get(IngestedRecord, ("searxng_results", "b" * 64))
        payload = dict(second.payload)
        payload["query"] = "Claude"
        second.payload = payload
        session.commit()

    listing = asyncio.run(_requests(engine, database_url, ["/ops/web-visibility?q=ChatGPT"]))[0]

    assert listing.status_code == 200 and listing.json()["total"] == 1
    item = listing.json()["items"][0]
    assert item["result_count"] == 2 and item["query_count"] == 2
    engine.dispose()


def _render_web_fixture() -> None:
    from omnisignal.ops_ui.visibility import render_visibility

    archive = "c" * 64
    detail = {
        "snapshot_id": archive,
        "validation_status": "valid",
        "observed_at": "2026-09-07T00:00:00Z",
        "older_than_24h": False,
        "result_count": 1,
        "query_count": 1,
        "engines": ["duckduckgo"],
        "quality_status": "complete",
        "is_example": True,
        "keyword_set": {"id": "example_ai_assistants", "version": 1},
        "entity_set": None,
        "access_tier": "anonymous_public",
        "scope": "fixture web search result snapshot scope",
        "raw_archive_sha256": archive,
        "slices": [
            {
                "query": "ChatGPT",
                "engine": "duckduckgo",
                "denominator": 1,
                "quality_status": "complete",
                "entities": [],
                "results": [
                    {
                        "position": 1,
                        "title": "ChatGPT",
                        "url": "https://chatgpt.com/",
                        "text_preview": "ChatGPT home",
                        "matches": [],
                    }
                ],
            }
        ],
    }

    def load(path: str, params=None):
        if path == "/ops/visibility":
            return {"total": 0, "items": [], "latest_source_run": None}
        if path == "/ops/web-visibility":
            return {"total": 1, "items": [{key: value for key, value in detail.items() if key != "slices"}]}
        return detail

    render_visibility(load)


def test_visibility_ui_switches_to_web_evidence_without_entity_configuration() -> None:
    app = AppTest.from_function(_render_web_fixture).run(timeout=10)
    app.radio[0].set_value("Web / SearXNG").run(timeout=10)

    assert not app.exception
    assert app.metric[0].value == "1"
    assert any("未绑定实体集" in info.value for info in app.info)
    assert app.dataframe[0].value.iloc[0]["标题"] == "ChatGPT"
