"""Allowlisted, persisted collection jobs executed outside the UI process."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
import yaml

from omnisignal.config import Settings
from omnisignal.contracts import ConnectorFailure, ConnectorSpec, ErrorCategory
from omnisignal.governance import load_source_approval
from omnisignal.measurement import AccessTier, MeasurementCatalog, ObservationContext, VersionRef
from omnisignal.storage import AuditEvent, CollectionJob


LOGGER = logging.getLogger("omnisignal.collection_jobs")
ACTIVE_STATUSES = ("pending", "running")
PAUSE_CATEGORIES = {
    ErrorCategory.AUTHENTICATION,
    ErrorCategory.PERMISSION,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.POLICY_VIOLATION,
    ErrorCategory.RESOURCE_EXHAUSTED,
    ErrorCategory.SCHEMA_DRIFT,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CollectionTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
    display_name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=3, max_length=240)
    connector: Literal["public_search_signals", "searxng_results", "youtube_visibility"]
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
    policy: str = Field(min_length=3, max_length=240)
    keyword_set: VersionRef
    entity_set: VersionRef | None
    access_tier: AccessTier
    observation_scope: str = Field(min_length=10, max_length=300)
    is_example: bool
    enabled: bool

    def definition_hash(self) -> str:
        payload = json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CollectionTaskDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    tasks: tuple[CollectionTask, ...] = Field(min_length=1, max_length=20)


class CollectionTaskCatalog:
    def __init__(self, path: Path, project_root: Path) -> None:
        self.path = path.resolve()
        self.project_root = project_root.resolve()
        self.profile_policies: dict[str, Path] = {}
        document = CollectionTaskDocument.model_validate(yaml.safe_load(self.path.read_text(encoding="utf-8")))
        self.measurements = MeasurementCatalog(
            self.project_root / "config" / "keyword_sets.yaml",
            self.project_root / "config" / "entity_sets.yaml",
        )
        by_id = {task.id: task for task in document.tasks}
        if len(by_id) != len(document.tasks):
            raise RuntimeError("collection task ids must be unique")
        for task in document.tasks:
            policy = self.resolve_policy(task)
            if not policy.is_file():
                raise RuntimeError(f"collection task policy is missing: {task.id}")
            self._validate_measurement_binding(task, policy)
        self.tasks = by_id

    def resolve_policy(self, task: CollectionTask) -> Path:
        if task.id in self.profile_policies:
            return self.profile_policies[task.id]
        value = Path(task.policy)
        if value.is_absolute():
            raise RuntimeError("collection task policy must be project-relative")
        resolved = (self.project_root / value).resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise RuntimeError("collection task policy escapes the project root") from exc
        return resolved

    def observation_context(self, task: CollectionTask) -> ObservationContext:
        return self.measurements.context(
            keyword_ref=task.keyword_set,
            entity_ref=task.entity_set,
            access_tier=task.access_tier,
            scope=task.observation_scope,
        )

    def _validate_measurement_binding(self, task: CollectionTask, policy_path: Path) -> None:
        query_set = self.measurements.keyword_set(task.keyword_set)
        entity_set = self.measurements.entity_set(task.entity_set)
        document = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        if task.connector == "public_search_signals":
            from omnisignal.connectors.public_search_policy import PublicSearchPolicy

            policy = PublicSearchPolicy.model_validate(document)
            if policy.keywords != query_set.queries:
                raise RuntimeError(f"collection task keyword set differs from policy: {task.id}")
            if entity_set is not None:
                raise RuntimeError(f"public search task does not accept an entity set: {task.id}")
            return

        if task.connector == "searxng_results":
            from omnisignal.connectors.searxng_policy import SearXNGPolicy

            policy = SearXNGPolicy.model_validate(document)
            if policy.queries != query_set.queries:
                raise RuntimeError(f"collection task keyword set differs from policy: {task.id}")
            return

        from omnisignal.connectors.youtube_visibility_policy import YouTubeVisibilityPolicy, normalized

        policy = YouTubeVisibilityPolicy.model_validate(document)
        if query_set.queries != (policy.query,):
            raise RuntimeError(f"collection task keyword set differs from policy: {task.id}")
        if entity_set is None:
            raise RuntimeError(f"youtube visibility task requires an entity set: {task.id}")
        policy_entities = {
            brand.id: (
                brand.name,
                brand.role,
                brand.aliases,
                brand.official_channel_ids,
            )
            for brand in policy.brands
        }
        catalog_entities = {
            entity.id: (
                entity.name,
                entity.role,
                tuple(normalized(alias) for alias in entity.aliases),
                entity.official_accounts.get("youtube", ()),
            )
            for entity in entity_set.entities
        }
        if policy_entities != catalog_entities:
            raise RuntimeError(f"collection task entity set differs from policy: {task.id}")


class CollectionExecutor:
    """A bounded one-worker queue; database rows remain the source of truth."""

    def __init__(
        self,
        *,
        engine: Engine,
        settings: Settings,
        catalog: CollectionTaskCatalog,
        registry_path: Path,
        project_root: Path,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.catalog = catalog
        self.registry_path = registry_path.resolve()
        self.project_root = project_root.resolve()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="omnisignal-collection")
        self._closed = False
        self._lock = threading.Lock()

    def reconcile_orphans(self) -> int:
        now = utc_now()
        with Session(self.engine) as session:
            jobs = session.scalars(select(CollectionJob).where(CollectionJob.status.in_(ACTIVE_STATUSES))).all()
            for job in jobs:
                job.status = "failed"
                job.error_code = "api_restarted"
                job.finished_at = now
                session.add(AuditEvent(actor="collection_job_manager", action="collection_job_orphaned",
                                       target=job.task_id, detail={"job_id": job.job_id, "error_code": "api_restarted"}))
            session.commit()
            return len(jobs)

    def submit(self, job_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("collection executor is closed")
            self._pool.submit(self._execute, job_id)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _execute(self, job_id: str) -> None:
        with Session(self.engine) as session:
            job = session.get(CollectionJob, job_id)
            if job is None or job.status != "pending":
                return
            job.status = "running"
            job.started_at = utc_now()
            session.commit()
            task = self.catalog.tasks.get(job.task_id)
            run_id = job.run_id

        if task is None or not task.enabled:
            self._finish_failure(job_id, "task_unavailable", paused=False)
            return
        connector = None
        try:
            connector = self._build_connector(task)
            from omnisignal.connectors.durable import run_connector_durably

            summary = asyncio.run(
                run_connector_durably(
                    connector,
                    database_url=self.settings.database_url,
                    database_target=self.settings.safe_database_target(),
                    initialize_schema=False,
                    run_id=run_id,
                )
            )
        except ConnectorFailure as exc:
            self._finish_failure(job_id, exc.category.value, paused=exc.category in PAUSE_CATEGORIES,
                                 diagnostics=getattr(connector, "last_diagnostics", []))
            return
        except Exception:
            LOGGER.exception("collection job failed: %s", job_id)
            self._finish_failure(job_id, ErrorCategory.RUNNER_CRASH.value, paused=False)
            return

        result = {
            "batches": summary.batches,
            "records_seen": summary.records_seen,
            "inserted": summary.inserted,
            "updated": summary.updated,
            "unchanged": summary.unchanged,
            "tombstoned": summary.tombstoned,
            "cycle_complete": summary.cycle_complete,
        }
        now = utc_now()
        with Session(self.engine) as session:
            job = session.get(CollectionJob, job_id)
            if job is None:
                return
            job.status = "succeeded"
            if task.connector == "searxng_results":
                diagnostics = getattr(connector, "last_diagnostics", [])
                result["diagnostics"] = diagnostics
                result["warnings"] = list(getattr(connector, "last_warnings", ()))
                result["coverage_status"] = (
                    ("partial" if summary.records_seen else "failed") if any(item["status"] in {"partial", "failed"} for item in diagnostics)
                    else "complete" if diagnostics else "unknown"
                )
            job.result_summary = result
            job.finished_at = now
            session.add(AuditEvent(actor="collection_job_manager", action="collection_job_succeeded",
                                   target=job.task_id, detail={"job_id": job.job_id, **result}))
            session.commit()

    def _finish_failure(self, job_id: str, error_code: str, *, paused: bool, diagnostics=None) -> None:
        with Session(self.engine) as session:
            job = session.get(CollectionJob, job_id)
            if job is None:
                return
            job.status = "paused" if paused else "failed"
            job.error_code = error_code
            if diagnostics:
                job.result_summary = {"coverage_status": "failed", "diagnostics": diagnostics}
            job.finished_at = utc_now()
            session.add(AuditEvent(actor="collection_job_manager", action="collection_job_stopped",
                                   target=job.task_id,
                                   detail={"job_id": job.job_id, "status": job.status, "error_code": error_code}))
            session.commit()

    def _build_connector(self, task: CollectionTask):
        spec_path = self.project_root / "examples" / "connectors" / f"{task.connector}.yaml"
        spec = ConnectorSpec.model_validate(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
        if spec.id != task.source_id:
            raise RuntimeError("collection task source does not match connector spec")
        approval = load_source_approval(self.registry_path, spec)
        policy_path = self.catalog.resolve_policy(task)
        workspace = self.project_root / "data" / "workers" / task.source_id
        archive_root = self.project_root / "data" / "raw" / task.source_id
        from omnisignal.connectors.archive import FileRawResponseArchive

        archive = FileRawResponseArchive(archive_root)
        observation_context = self.catalog.observation_context(task)
        if task.connector == "public_search_signals":
            from omnisignal.connectors.public_search_policy import PublicSearchPolicy
            from omnisignal.connectors.public_search_signals import PublicSearchSignalsConnector

            policy = PublicSearchPolicy.model_validate(yaml.safe_load(policy_path.read_text(encoding="utf-8")))
            return PublicSearchSignalsConnector(
                spec,
                policy,
                approval,
                workspace=workspace,
                archive=archive,
                observation_context=observation_context,
            )
        if task.connector == "searxng_results":
            from omnisignal.connectors.searxng_policy import SearXNGPolicy
            from omnisignal.connectors.searxng_results import SearXNGResultsConnector

            policy = SearXNGPolicy.model_validate(yaml.safe_load(policy_path.read_text(encoding="utf-8")))
            return SearXNGResultsConnector(
                spec,
                policy,
                approval,
                workspace=workspace,
                archive=archive,
                observation_context=observation_context,
            )
        from omnisignal.connectors.youtube_visibility import YouTubeVisibilityConnector
        from omnisignal.connectors.youtube_visibility_policy import YouTubeVisibilityPolicy

        policy = YouTubeVisibilityPolicy.model_validate(yaml.safe_load(policy_path.read_text(encoding="utf-8")))
        return YouTubeVisibilityConnector(
            spec,
            policy,
            approval,
            workspace=workspace,
            archive=archive,
            observation_context=observation_context,
        )
