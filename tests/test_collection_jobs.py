from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from omnisignal.api import create_app
from omnisignal.api.auth import ControlPrincipal, hash_control_token
from omnisignal.collection_jobs import CollectionExecutor, CollectionTaskCatalog
from omnisignal.collection_readiness import TaskReadiness
from omnisignal.config import Environment, Settings
from omnisignal.ops_ui.client import OpsApiClient, OpsApiError
from omnisignal.storage import AuditEvent, Base, CollectionJob, SourceControlState, create_database_engine
from omnisignal.contracts import ConnectorSpec
from omnisignal.storage import IngestionRun
from omnisignal.testing import FixtureConnector
import yaml


ROOT = Path(__file__).parents[1]
TOKEN = "collection-operator-token-with-more-than-32-characters"


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.reconciled = 0

    def reconcile_orphans(self) -> int:
        self.reconciled += 1
        return 0

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)


class FakeReadiness:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, bool]] = []

    def check(self, task, *, force: bool = False) -> TaskReadiness:
        self.calls.append((task.id, force))
        if self.available:
            return TaskReadiness("ready", True, "fixture_ready", "fixture ready", datetime.now(timezone.utc))
        return TaskReadiness(
            "unavailable",
            False,
            "dependency_unavailable",
            "dependency unavailable",
            datetime.now(timezone.utc),
        )


def _principal() -> ControlPrincipal:
    return ControlPrincipal(actor="collection-ops", role="operator", token_sha256=hash_control_token(TOKEN))


def _app(tmp_path: Path, executor: FakeExecutor, readiness: FakeReadiness | None = None):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(environment=Environment.TEST, database_url=database_url)
    return create_app(
        settings=settings,
        engine=engine,
        registry_path=ROOT / "governance" / "source_registry.yaml",
        collection_tasks_path=ROOT / "config" / "collection_tasks.yaml",
        search_profiles_path=tmp_path / "profiles",
        collection_executor=executor,
        collection_readiness=readiness or FakeReadiness(),
        control_principals=(_principal(),),
    ), engine


def test_collection_api_is_authenticated_idempotent_and_allowlisted(tmp_path: Path) -> None:
    executor = FakeExecutor()
    app, engine = _app(tmp_path, executor)
    path = "/ops/collection-jobs"
    task_id = "public_search_signals_example"
    body = {"task_id": task_id, "confirmation": f"RUN {task_id}"}
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "collection-request-000001"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                tasks = await client.get("/ops/collection-tasks")
                missing_auth = await client.post(path, json=body, headers={"Idempotency-Key": "collection-request-000000"})
                bad_confirm = await client.post(path, json={**body, "confirmation": "yes"}, headers=headers)
                queued = await client.post(path, json=body, headers=headers)
                replay = await client.post(path, json=body, headers=headers)
                changed = await client.post(
                    path,
                    json={"task_id": "youtube_visibility_example", "confirmation": "RUN youtube_visibility_example"},
                    headers=headers,
                )
                jobs = await client.get(path)
                return tasks, missing_auth, bad_confirm, queued, replay, changed, jobs

    tasks, missing_auth, bad_confirm, queued, replay, changed, jobs = asyncio.run(scenario())
    assert tasks.status_code == 200 and tasks.json()["count"] == 3
    assert all(item["readiness"]["available"] is True for item in tasks.json()["items"])
    assert missing_auth.status_code == 401
    assert bad_confirm.status_code == 400
    assert queued.status_code == 202 and queued.json()["status"] == "pending"
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    assert changed.status_code == 409
    assert jobs.json()["count"] == 1
    assert executor.submitted == [queued.json()["job_id"]]
    with Session(engine) as session:
        assert len(session.scalars(select(CollectionJob)).all()) == 1
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "collection_job_queued")).all()
        assert len(events) == 1 and TOKEN not in str(events[0].detail)
    engine.dispose()


def test_unavailable_dependency_is_rejected_before_queue_and_recovery_can_submit(tmp_path: Path) -> None:
    executor = FakeExecutor()
    readiness = FakeReadiness(available=False)
    app, engine = _app(tmp_path, executor, readiness)
    task_id = "searxng_results_example"
    body = {"task_id": task_id, "confirmation": f"RUN {task_id}"}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                listing = await client.get("/ops/collection-tasks")
                blocked = await client.post(
                    "/ops/collection-jobs",
                    json=body,
                    headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "unavailable-request-0001"},
                )
                readiness.available = True
                recovered = await client.post(
                    "/ops/collection-jobs",
                    json=body,
                    headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "recovered-request-00001"},
                )
                return listing, blocked, recovered

    listing, blocked, recovered = asyncio.run(scenario())
    item = next(item for item in listing.json()["items"] if item["task_id"] == task_id)
    assert item["readiness"]["available"] is False
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "dependency_unavailable"
    assert recovered.status_code == 202
    assert executor.submitted == [recovered.json()["job_id"]]
    with Session(engine) as session:
        assert len(session.scalars(select(CollectionJob)).all()) == 1
    engine.dispose()


def _render_collection_fixture(available: bool) -> None:
    from omnisignal.ops_ui.collection import render_collection

    task = {
        "task_id": "searxng_results_example",
        "display_name": "SearXNG sample",
        "description": "fixture task",
        "source_id": "searxng_results",
        "keyword_set": {"id": "example_ai_assistants", "version": 1},
        "entity_set": None,
        "access_tier": "anonymous_public",
        "observation_scope": "fixture observation scope",
        "is_example": True,
        "enabled": True,
        "readiness": {
            "status": "ready" if available else "unavailable",
            "available": available,
            "detail_code": "searxng_ready" if available else "dependency_unavailable",
            "message": "本机 SearXNG 已就绪。" if available else "请先启动 Docker Desktop 和 SearXNG。",
            "checked_at": "2026-09-07T00:00:00Z",
        },
    }

    def load(path: str, params=None):
        if path == "/ops/collection-tasks":
            return {"items": [task], "count": 1}
        if path == "/ops/collection-jobs":
            return {"items": [], "count": 0}
        return {"auto_start": False, "runtime_state": "disabled_by_environment", "items": [], "count": 0}

    render_collection(load, run=lambda task_id: {"job_id": "fixture-job"})


def test_collection_ui_disables_unavailable_dependency_and_explains_recovery() -> None:
    unavailable = AppTest.from_function(_render_collection_fixture, args=(False,)).run(timeout=10)
    ready = AppTest.from_function(_render_collection_fixture, args=(True,)).run(timeout=10)

    assert not unavailable.exception and unavailable.button[0].disabled is True
    assert any("Docker Desktop" in item.value for item in unavailable.error)
    assert not ready.exception and ready.button[0].disabled is False
    assert any("SearXNG 已就绪" in item.value for item in ready.success)


def test_disabled_source_is_rejected_before_queue(tmp_path: Path) -> None:
    executor = FakeExecutor()
    app, engine = _app(tmp_path, executor)
    with Session(engine) as session:
        session.add(SourceControlState(source_id="youtube_visibility", enabled=False, version=1,
                                       updated_by="ops", reason="maintenance"))
        session.commit()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/ops/collection-jobs",
                    json={"task_id": "youtube_visibility_example", "confirmation": "RUN youtube_visibility_example"},
                    headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "collection-disabled-0001"},
                )

    response = asyncio.run(scenario())
    assert response.status_code == 409
    assert executor.submitted == []
    engine.dispose()


def test_executor_reconciles_orphaned_jobs_after_restart(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'orphan.db').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    catalog = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    executor = CollectionExecutor(
        engine=engine,
        settings=Settings(environment=Environment.TEST, database_url=database_url),
        catalog=catalog,
        registry_path=ROOT / "governance" / "source_registry.yaml",
        project_root=ROOT,
    )
    with Session(engine) as session:
        session.add(CollectionJob(job_id="a" * 36, command_hash="b" * 64, request_hash="c" * 64,
                                  task_id="public_search_signals_example", task_definition_hash="d" * 64,
                                  source_id="public_search_signals", run_id="e" * 36, status="running",
                                  actor="ops", result_summary={}))
        session.commit()
    try:
        assert executor.reconcile_orphans() == 1
        with Session(engine) as session:
            job = session.get(CollectionJob, "a" * 36)
            assert job is not None and job.status == "failed" and job.error_code == "api_restarted"
    finally:
        executor.shutdown()
        engine.dispose()


def test_executor_runs_allowlisted_job_through_durable_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'execute.db').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    catalog = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    executor = CollectionExecutor(
        engine=engine,
        settings=Settings(environment=Environment.TEST, database_url=database_url),
        catalog=catalog,
        registry_path=ROOT / "governance" / "source_registry.yaml",
        project_root=ROOT,
    )
    spec = ConnectorSpec.model_validate(yaml.safe_load(
        (ROOT / "examples/connectors/public_search_signals.yaml").read_text(encoding="utf-8")
    ))
    monkeypatch.setattr(executor, "_build_connector", lambda task: FixtureConnector(spec, [{"id": "one"}]))
    job_id, run_id = "1" * 36, "5" * 36
    job = CollectionJob(job_id=job_id, command_hash="2" * 64, request_hash="3" * 64,
                        task_id="public_search_signals_example", task_definition_hash="4" * 64,
                        source_id="public_search_signals", run_id=run_id, status="pending",
                        actor="ops", result_summary={})
    with Session(engine) as session:
        session.add(job)
        session.commit()
    try:
        executor.submit(job_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with Session(engine) as session:
                status_value = session.get(CollectionJob, job_id).status
            if status_value not in {"pending", "running"}:
                break
            time.sleep(0.02)
        with Session(engine) as session:
            persisted = session.get(CollectionJob, job_id)
            run = session.get(IngestionRun, run_id)
            assert persisted is not None and persisted.status == "succeeded"
            assert persisted.result_summary["records_seen"] == 1
            assert run is not None and run.run_id == persisted.run_id
    finally:
        executor.shutdown()
        engine.dispose()


def test_collection_client_never_retries_write_and_sends_confirmation(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("http://127.0.0.1:8010/ops/collection-jobs").respond(503)
    with pytest.raises(OpsApiError):
        OpsApiClient(retries=2).start_collection_job(
            task_id="public_search_signals_example",
            idempotency_key="collection-client-000001",
            bearer_token=TOKEN,
        )
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Idempotency-Key"] == "collection-client-000001"
    assert request.content == b'{"task_id":"public_search_signals_example","confirmation":"RUN public_search_signals_example"}'


def test_catalog_rejects_policy_path_escape(tmp_path: Path) -> None:
    path = tmp_path / "tasks.yaml"
    path.write_text(
        """version: 1
tasks:
  - id: unsafe_task
    display_name: Unsafe task
    description: Invalid traversal fixture
    connector: public_search_signals
    source_id: public_search_signals
    policy: ../outside.yaml
    keyword_set:
      id: example_ai_assistants
      version: 1
    entity_set: null
    access_tier: anonymous_public
    observation_scope: Deliberately invalid path traversal test observation scope.
    is_example: true
    enabled: true
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="escapes"):
        CollectionTaskCatalog(path, ROOT)
