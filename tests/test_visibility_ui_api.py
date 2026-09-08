from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import httpx
import pytest
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from omnisignal.api import create_app
from omnisignal.api.visibility import project
from omnisignal.config import Settings, Environment
from omnisignal.connectors.youtube_visibility_policy import YouTubeVisibilityPolicy
from omnisignal.connectors.youtube_visibility_analysis import SearchSample, analyze_sample
from omnisignal.storage import Base, IngestedRecord, create_database_engine


def record():
    policy = YouTubeVisibilityPolicy(query="test query", top_k=2, brands=[
        {"id": "own", "name": "Alpha", "role": "owned", "aliases": ["Alpha"]}])
    sample = SearchSample(query=policy.query, top_k=2, fetched_at="2026-01-01T00:00:00Z", collector_version="fixture/1",
        entries=[{"position": 1, "video_id": "aaaaaaaaaaa", "title": "Alpha review"},
                 {"position": 2, "video_id": "bbbbbbbbbbb", "title": "Other"}])
    report = analyze_sample(policy, sample)
    return IngestedRecord(source_id="youtube_visibility", source_record_id="a" * 64,
        payload={"visibility_snapshot": report}, raw_hash="b" * 64, schema_version="1.0",
        permission="fixture", raw_archive_sha256="c" * 64)


async def requests(engine, paths):
    app = create_app(settings=Settings(environment=Environment.TEST, database_url="sqlite://"), engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return [await client.get(path) for path in paths]


def seeded(tmp_path, row):
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(row)
        session.commit()
    return engine


def test_api_bounded_projection_and_safe_evidence_links(tmp_path):
    row = record()
    snapshot = row.payload["visibility_snapshot"]
    snapshot["private_token"] = "must-not-leak"
    snapshot["evidence"][0]["url"] = "javascript:alert(1)"
    snapshot["evidence"][0]["matches"][0]["private_token"] = "must-not-leak"
    engine = seeded(tmp_path, row)
    responses = asyncio.run(requests(engine, ["/ops/visibility", "/ops/visibility/" + "a" * 64,
        "/ops/visibility?limit=51", "/ops/visibility?offset=100001", "/ops/visibility?q=absent", "/ops/visibility?q=%25"]))
    listing, detail, limit, offset, empty, wildcard = responses
    assert listing.status_code == detail.status_code == 200
    assert "evidence" not in listing.json()["items"][0]
    result = detail.json()
    assert result["brands"][0]["sample_share_percent"] == 50
    assert result["older_than_24h"] is True
    assert result["evidence"][0]["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert "must-not-leak" not in detail.text and "javascript:" not in detail.text
    assert limit.status_code == offset.status_code == 422
    assert empty.json()["total"] == wildcard.json()["total"] == 0
    engine.dispose()


def test_invalid_counters_are_not_published(tmp_path):
    row = record()
    row.payload["visibility_snapshot"]["brands"][0]["sample_share_percent"] = 100
    engine = seeded(tmp_path, row)
    listing, detail = asyncio.run(requests(engine, ["/ops/visibility", "/ops/visibility/" + "a" * 64]))
    assert listing.json()["items"][0]["validation_status"] == "invalid_snapshot"
    assert detail.status_code == 409
    assert "100" not in detail.text
    engine.dispose()


def test_deleted_snapshots_are_hidden(tmp_path):
    row = record()
    row.deleted_at = datetime.now(timezone.utc)
    engine = seeded(tmp_path, row)
    listing, detail = asyncio.run(requests(engine, ["/ops/visibility", "/ops/visibility/" + "a" * 64]))
    assert listing.json()["total"] == 0
    assert detail.status_code == 404
    engine.dispose()


def _render_fixture(detail, mode="normal"):
    import streamlit as st
    from omnisignal.ops_ui.visibility import render_visibility
    unavailable = st.checkbox("模拟 API 断开", value=mode == "failure")
    def load(path, params=None):
        if unavailable:
            st.error("API 不可用")
            return None
        if path == "/ops/visibility":
            return {"total": 0 if mode == "empty" else 1, "items": [] if mode == "empty" else [detail],
                    "latest_source_run": {"status": "failed"}}
        return detail
    render_visibility(load)


def test_ui_renders_counts_example_staleness_and_removes_old_data_on_failure():
    detail = project(record(), detail=True)
    app = AppTest.from_function(_render_fixture, args=(detail,)).run(timeout=10)
    assert not app.exception
    assert app.metric[0].value == "2 / 2"
    assert app.dataframe[0].value.iloc[0]["采样占比"] == "50%"
    assert any("示例品牌" in warning.value for warning in app.warning)
    assert any("24 小时" in warning.value for warning in app.warning)
    assert any("未成功" in warning.value for warning in app.warning)
    app.checkbox[0].check().run()
    assert not app.exception and app.error
    assert not app.dataframe and not app.metric


@pytest.mark.parametrize("mode", ["empty", "failure", "incomplete"])
def test_ui_empty_failure_incomplete(mode):
    detail = project(record(), detail=True)
    if mode == "incomplete":
        detail["quality_status"] = "incomplete"
        # Even an inconsistent upstream response must not display a percentage for an incomplete sample.
    app = AppTest.from_function(_render_fixture, args=(detail, mode)).run(timeout=10)
    assert not app.exception
    if mode == "incomplete":
        assert app.dataframe[0].value.iloc[0]["采样占比"] == "不可计算"
    else:
        assert not app.metric and not app.dataframe
