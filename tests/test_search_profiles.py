from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from omnisignal.api import create_app
from omnisignal.api.auth import ControlPrincipal, hash_control_token
from omnisignal.collection_jobs import CollectionTaskCatalog, CollectionExecutor
from omnisignal.config import Environment, Settings
from omnisignal.search_profiles import SearchProfile, SearchProfileStore
from omnisignal.storage import Base, CollectionJob, AuditEvent, create_database_engine
from test_collection_jobs import FakeExecutor, FakeReadiness

ROOT = Path(__file__).parents[1]
TOKEN = "test-profile-operator-secret-at-least-32-characters"


def catalog():
    return CollectionTaskCatalog(ROOT / "config/collection_tasks.yaml", ROOT)


def definition(**updates):
    return {"name": "", "queries": ["降噪耳机"], "engines": ["duckduckgo", "brave"],
            "products": [{"name": "自家耳机", "role": "owned", "aliases": ["耳机甲"], "domains": ["example.com"]}],
            **updates}


def test_store_restart_idempotency_and_immutable_edits(tmp_path):
    first_catalog = catalog()
    store = SearchProfileStore(tmp_path / "profiles", first_catalog)
    profile = SearchProfile(**definition())
    first = store.save(profile)
    assert first["profile"]["name"] == "降噪耳机"
    assert store.save(profile)["replayed"] is True
    old_task = first_catalog.tasks[first["task_id"]]
    old_context = first_catalog.observation_context(old_task)
    old_policy = first_catalog.resolve_policy(old_task).read_bytes()
    second = store.save(SearchProfile(**definition(queries=["另一关键词"])))
    assert first["task_id"] != second["task_id"]
    restarted = catalog()
    loaded = SearchProfileStore(tmp_path / "profiles", restarted)
    assert loaded.list()["count"] == 2
    assert restarted.observation_context(restarted.tasks[first["task_id"]]) == old_context
    assert restarted.resolve_policy(old_task).read_bytes() == old_policy
    assert not list((tmp_path / "profiles").glob(".pending-*"))


@pytest.mark.parametrize("change", [
    {"queries": []}, {"queries": ["same", "SAME"]}, {"queries": ["x"] * 11},
    {"engines": []}, {"engines": ["arbitrary"]}, {"engines": ["brave", "brave"]},
    {"endpoint": "http://internal"}, {"policy": "../../secret"}, {"name": "bad\nname"},
    {"products": [{"name": "A", "role": "owned", "domains": ["https://example.com"]}]},
    {"products": [{"name": "A", "role": "owned", "domains": ["bad host.com"]}]},
    {"products": [{"name": "A", "role": "owned", "domains": ["*.example.com"]}]},
    {"products": [{"name": "A", "role": "owned", "aliases": [str(i) + "字" * 98 for i in range(29)]}]},
    {"products": [{"name": "A", "role": "owned"}, {"name": "A", "role": "competitor"}]},
])
def test_invalid_profiles_rejected(change):
    with pytest.raises(ValidationError):
        SearchProfile(**definition(**change))


def test_atomic_failure_does_not_publish(tmp_path, monkeypatch):
    store = SearchProfileStore(tmp_path / "profiles", catalog())
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("omnisignal.search_profiles.os.fsync", fail)
    with pytest.raises(OSError):
        store.save(SearchProfile(**definition()))
    assert store.list()["count"] == 0
    assert len(store.catalog.tasks) == 3


def test_readiness_shared_but_manual_run_is_fresh(tmp_path):
    from omnisignal.collection_readiness import CollectionReadinessService
    active = catalog()
    store = SearchProfileStore(tmp_path / "profiles", active)
    one = store.save(SearchProfile(**definition()))["task_id"]
    two = store.save(SearchProfile(**definition(queries=["another"])))["task_id"]
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"version": "test", "engines": [
            {"name": "brave", "enabled": True}, {"name": "duckduckgo", "enabled": True}]})
    service = CollectionReadinessService(active, transport=httpx.MockTransport(handler))
    assert service.check(active.tasks[one]).available
    assert service.check(active.tasks[two]).available
    assert len(requests) == 1
    assert service.check(active.tasks[two], force=True).available
    assert len(requests) == 2


def test_saved_profile_builds_existing_connector(tmp_path):
    active = catalog()
    store = SearchProfileStore(tmp_path / "profiles", active)
    saved = store.save(SearchProfile(**definition()))
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    executor = CollectionExecutor(engine=engine, settings=Settings(environment=Environment.TEST, database_url="sqlite+pysqlite:///:memory:"),
                                  catalog=active, registry_path=ROOT / "governance/source_registry.yaml",
                                  project_root=ROOT)
    try:
        connector = executor._build_connector(active.tasks[saved["task_id"]])
        assert connector is not None
        asyncio.run(connector.validate())
        from omnisignal.contracts import Checkpoint, ConnectorFailure
        from datetime import datetime, timezone
        completed = Checkpoint(source_id="searxng_results", value={"policy_hash": "old", "cycle_complete": True},
                               version=1, updated_at=datetime.now(timezone.utc))
        connector._validate_checkpoint(completed)
        with pytest.raises(ConnectorFailure):
            connector._validate_checkpoint(completed.model_copy(update={"value": {"policy_hash": "old", "cycle_complete": False}}))
        with pytest.raises(ConnectorFailure):
            connector._validate_checkpoint(completed.model_copy(update={"source_id": "other_source"}))
    finally:
        executor.shutdown()


def test_api_save_does_not_run_and_requires_operator(tmp_path):
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}")
    Base.metadata.create_all(engine)
    executor = FakeExecutor()
    viewer_token = TOKEN + "-viewer"
    app = create_app(engine=engine, settings=Settings(environment=Environment.TEST, database_url=str(engine.url)),
                     search_profiles_path=tmp_path / "profiles", collection_executor=executor,
                     collection_readiness=FakeReadiness(), scheduler_runtime_enabled=False,
                     control_principals=(ControlPrincipal(actor="test", role="operator", token_sha256=hash_control_token(TOKEN)),
                                         ControlPrincipal(actor="viewer", role="viewer", token_sha256=hash_control_token(viewer_token))))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            endpoint = "/ops/search-profiles"
            assert (await client.post(endpoint, json=definition())).status_code == 401
            assert (await client.post(endpoint, json=definition(), headers={"Authorization": f"Bearer {viewer_token}"})).status_code == 403
            headers = {"Authorization": f"Bearer {TOKEN}"}
            saved = await client.post(endpoint, json=definition(), headers=headers)
            assert saved.status_code == 200
            task_id = saved.json()["task_id"]
            assert (await client.post(endpoint, json=definition(), headers=headers)).json()["replayed"] is True
            assert (await client.get(endpoint)).json()["count"] == 1
            assert executor.submitted == []
            assert (await client.get("/ops/collection-jobs")).json()["count"] == 0
            tasks = (await client.get("/ops/collection-tasks")).json()["items"]
            assert any(item["task_id"] == task_id for item in tasks)
            schedule = (await client.get("/ops/snapshot-schedules")).json()
            assert schedule["auto_start"] is False
            response = await client.post("/ops/collection-jobs", json={"task_id": task_id, "confirmation": f"RUN {task_id}"},
                                         headers={**headers, "Idempotency-Key": "profile-run-test-00000001"})
            assert response.status_code == 202
            assert len(executor.submitted) == 1
    asyncio.run(scenario())
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CollectionJob)) == 1
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "search_profile_saved")).all()
        assert len(events) == 2
        assert TOKEN not in str([event.detail for event in events])


def editor_fixture():
    import streamlit as st
    from omnisignal.ops_ui.search_profiles import render_profile_editor
    def load(path, params):
        return {"items": [], "count": 0}
    def save(body):
        st.session_state["saved_body"] = body
        return {"task_id": "fixture"}
    render_profile_editor(load, save)


def test_tag_editor_save_and_validation():
    app = AppTest.from_function(editor_fixture).run()
    assert not app.exception
    app.button[0].click().run()
    assert app.error
    assert "saved_body" not in app.session_state
    app.multiselect[0].set_value(["耳机"]).run()
    app.multiselect[2].set_value(["耳机甲"]).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    body = app.session_state["saved_body"]
    assert body["name"] == "耳机"
    assert body["products"][0]["name"] == "耳机甲"
    assert any("未触发采集" in item.value for item in app.success)
