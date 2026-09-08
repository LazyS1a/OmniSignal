"""FastAPI application factory. Importing this module has no side effects."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from omnisignal.config import Settings
from omnisignal.collection_jobs import CollectionExecutor, CollectionTaskCatalog
from omnisignal.collection_readiness import CollectionReadinessService
from omnisignal.logging import configure_logging
from omnisignal.snapshot_schedules import SnapshotScheduleCatalog, SnapshotSchedulerService
from omnisignal.storage import create_database_engine
from omnisignal.storage.readiness import assert_schema_ready, expected_heads

from .auth import ControlPrincipal, load_control_principals
from .collection import router as collection_router
from .control import router as control_router
from .ops import router as ops_router
from .visibility import router as visibility_router
from .web_visibility import router as web_visibility_router
from .trends import router as trends_router


LOGGER = logging.getLogger("omnisignal.api")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    registry_path: Path | None = None,
    control_principals: tuple[ControlPrincipal, ...] | None = None,
    collection_tasks_path: Path | None = None,
    snapshot_schedules_path: Path | None = None,
    scheduler_runtime_enabled: bool | None = None,
    collection_executor: object | None = None,
    collection_readiness: object | None = None,
    search_profiles_path: Path | None = None,
) -> FastAPI:
    active_settings = settings or Settings.from_env()
    configure_logging(active_settings.log_level)
    active_engine = engine or create_database_engine(active_settings.database_url)
    schema_heads = expected_heads()
    active_registry_path = registry_path or PROJECT_ROOT / "governance" / "source_registry.yaml"
    active_catalog = CollectionTaskCatalog(
        collection_tasks_path or PROJECT_ROOT / "config" / "collection_tasks.yaml",
        PROJECT_ROOT,
    )
    active_schedule_catalog = SnapshotScheduleCatalog(
        snapshot_schedules_path or PROJECT_ROOT / "config" / "snapshot_schedules.yaml",
        task_ids=set(active_catalog.tasks),
    )
    from omnisignal.search_profiles import SearchProfileStore
    profile_store = SearchProfileStore(search_profiles_path or PROJECT_ROOT / "data" / "search_profiles", active_catalog)
    owns_collection_executor = collection_executor is None
    active_collection_executor = collection_executor or CollectionExecutor(
        engine=active_engine,
        settings=active_settings,
        catalog=active_catalog,
        registry_path=active_registry_path,
        project_root=PROJECT_ROOT,
    )
    active_collection_readiness = collection_readiness or CollectionReadinessService(active_catalog)
    runtime_scheduler_gate = (
        os.getenv("OMNISIGNAL_SCHEDULER_ENABLED", "").strip().lower() == "true"
        if scheduler_runtime_enabled is None
        else scheduler_runtime_enabled
    )
    active_snapshot_scheduler = SnapshotSchedulerService(
        engine=active_engine,
        schedule_catalog=active_schedule_catalog,
        collection_catalog=active_catalog,
        collection_executor=active_collection_executor,
        registry_path=active_registry_path,
        project_root=PROJECT_ROOT,
        runtime_enabled=runtime_scheduler_gate,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        LOGGER.info("service started: %s", active_settings.safe_database_target())
        inspector = inspect(active_engine, raiseerr=False)
        if inspector is not None and inspector.has_table("collection_jobs"):
            active_collection_executor.reconcile_orphans()
            active_snapshot_scheduler.start()
        try:
            yield
        finally:
            active_snapshot_scheduler.shutdown()
            if owns_collection_executor:
                active_collection_executor.shutdown()
            active_engine.dispose()
            LOGGER.info("service stopped")

    app = FastAPI(
        title="OmniSignal API",
        version=active_settings.service_version,
        docs_url="/docs" if active_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.engine = active_engine
    app.state.registry_path = active_registry_path
    app.state.collection_catalog = active_catalog
    app.state.search_profiles = profile_store
    app.state.snapshot_schedule_catalog = active_schedule_catalog
    app.state.snapshot_scheduler = active_snapshot_scheduler
    app.state.collection_executor = active_collection_executor
    app.state.collection_readiness = active_collection_readiness
    app.state.control_principals = (
        load_control_principals() if control_principals is None else control_principals
    )
    app.include_router(ops_router)
    app.include_router(visibility_router)
    app.include_router(web_visibility_router)
    app.include_router(trends_router)
    app.include_router(control_router)
    app.include_router(collection_router)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError) -> Response:
        LOGGER.warning("database request failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "Operational data is temporarily unavailable."},
            headers={"Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive", "service": active_settings.service_name}

    @app.get("/health/ready")
    def ready() -> Response:
        dependency = "database"
        try:
            with active_engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                dependency = "schema"
                assert_schema_ready(connection, schema_heads)
        except Exception as exc:
            LOGGER.warning("readiness check failed: %s", type(exc).__name__)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "dependency": dependency},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(content={"status": "ready"}, headers={"Cache-Control": "no-store"})

    return app
