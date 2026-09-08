"""Pure schedule policy and due-time calculation; this module starts no background work."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import yaml

from omnisignal.contracts import ConnectorFailure, ConnectorSpec
from omnisignal.governance import load_source_approval
from omnisignal.storage import AuditEvent, CollectionJob, assert_source_enabled


LOGGER = logging.getLogger("omnisignal.snapshot_schedules")


class SnapshotSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    display_name: str = Field(min_length=3, max_length=100)
    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
    status: Literal["paused", "active"]
    interval_minutes: int = Field(ge=60, le=525_600, strict=True)
    anchor_utc: datetime

    @field_validator("anchor_utc")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("schedule anchor must include a timezone")
        return value.astimezone(timezone.utc)

    def next_after(self, value: datetime) -> datetime:
        current = _utc(value)
        if current < self.anchor_utc:
            return self.anchor_utc
        interval = timedelta(minutes=self.interval_minutes)
        elapsed = current - self.anchor_utc
        completed = elapsed // interval
        return self.anchor_utc + interval * (completed + 1)


class SnapshotScheduleDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    auto_start: bool
    schedules: tuple[SnapshotSchedule, ...] = Field(max_length=50)


class SnapshotScheduleCatalog:
    def __init__(self, path: Path, *, task_ids: set[str]) -> None:
        self.path = path.resolve()
        self.document = SnapshotScheduleDocument.model_validate(
            yaml.safe_load(self.path.read_text(encoding="utf-8"))
        )
        self.schedules = {schedule.id: schedule for schedule in self.document.schedules}
        if len(self.schedules) != len(self.document.schedules):
            raise RuntimeError("snapshot schedule ids must be unique")
        missing = sorted({schedule.task_id for schedule in self.document.schedules} - task_ids)
        if missing:
            raise RuntimeError(f"snapshot schedules reference unknown tasks: {', '.join(missing)}")

    @property
    def auto_start(self) -> bool:
        return self.document.auto_start

    def is_due(self, schedule: SnapshotSchedule, *, now: datetime,
               last_triggered_at: datetime | None) -> bool:
        if not self.auto_start or schedule.status != "active":
            return False
        current = _utc(now)
        if last_triggered_at is None:
            return current >= schedule.anchor_utc
        return schedule.next_after(_utc(last_triggered_at)) <= current

    def due_schedule_ids(self, *, now: datetime,
                         last_triggered_at: dict[str, datetime | None]) -> tuple[str, ...]:
        return tuple(
            schedule.id
            for schedule in self.document.schedules
            if self.is_due(schedule, now=now, last_triggered_at=last_triggered_at.get(schedule.id))
        )


class SnapshotSchedulerService:
    """Optional daemon that queues existing allowlisted jobs; disabled unless all gates are open."""

    def __init__(self, *, engine: Engine, schedule_catalog: SnapshotScheduleCatalog,
                 collection_catalog: object, collection_executor: object,
                 registry_path: Path, project_root: Path,
                 runtime_enabled: bool, poll_seconds: float = 60.0) -> None:
        if not 1 <= poll_seconds <= 3600:
            raise ValueError("scheduler poll interval must be between 1 and 3600 seconds")
        self.engine = engine
        self.schedule_catalog = schedule_catalog
        self.collection_catalog = collection_catalog
        self.collection_executor = collection_executor
        self.registry_path = registry_path.resolve()
        self.project_root = project_root.resolve()
        self.runtime_enabled = runtime_enabled
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def state(self) -> str:
        if not self.runtime_enabled:
            return "disabled_by_environment"
        if not self.schedule_catalog.auto_start:
            return "disabled_by_config"
        if self._thread is not None and self._thread.is_alive():
            return "running"
        return "enabled_not_started"

    def start(self) -> bool:
        if not self.runtime_enabled or not self.schedule_catalog.auto_start:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="omnisignal-snapshot-scheduler", daemon=True)
        self._thread.start()
        return True

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=min(self.poll_seconds + 1, 5))

    def tick(self, *, now: datetime | None = None) -> tuple[str, ...]:
        if not self.runtime_enabled or not self.schedule_catalog.auto_start:
            return ()
        current = _utc(now or datetime.now(timezone.utc))
        last_triggered = self._last_triggered()
        due_ids = self.schedule_catalog.due_schedule_ids(now=current, last_triggered_at=last_triggered)
        queued: list[str] = []
        for schedule_id in due_ids:
            schedule = self.schedule_catalog.schedules[schedule_id]
            job_id = self._queue(schedule, current)
            if job_id is None:
                continue
            try:
                self.collection_executor.submit(job_id)
            except RuntimeError:
                self._mark_queue_failure(job_id)
                continue
            queued.append(job_id)
        return tuple(queued)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("snapshot scheduler tick failed")
            self._stop.wait(self.poll_seconds)

    def _last_triggered(self) -> dict[str, datetime | None]:
        result: dict[str, datetime | None] = {}
        with Session(self.engine) as session:
            for schedule in self.schedule_catalog.schedules.values():
                actor = self._actor(schedule.id)
                stored = session.scalar(
                    select(CollectionJob.created_at)
                    .where(CollectionJob.actor == actor)
                    .order_by(CollectionJob.created_at.desc(), CollectionJob.job_id.desc())
                    .limit(1)
                )
                result[schedule.id] = (
                    stored.replace(tzinfo=timezone.utc) if stored is not None and stored.tzinfo is None else stored
                )
        return result

    def _queue(self, schedule: SnapshotSchedule, now: datetime) -> str | None:
        task = self.collection_catalog.tasks.get(schedule.task_id)
        if task is None or not task.enabled:
            return None
        try:
            spec_path = self.project_root / "examples" / "connectors" / f"{task.connector}.yaml"
            spec = ConnectorSpec.model_validate(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
            if spec.id != task.source_id:
                return None
            load_source_approval(self.registry_path, spec)
        except Exception:
            LOGGER.warning("scheduled task source approval is unavailable: %s", schedule.id)
            return None
        slot = self._current_slot(schedule, now)
        actor = self._actor(schedule.id)
        command_hash = _sha256(f"{actor}\0{_iso(slot)}")
        request_hash = _sha256(json.dumps({
            "schedule_id": schedule.id,
            "task_id": task.id,
            "task_definition_hash": task.definition_hash(),
            "scheduled_for": _iso(slot),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        with Session(self.engine) as session:
            if session.scalar(select(CollectionJob).where(CollectionJob.command_hash == command_hash)) is not None:
                return None
            try:
                assert_source_enabled(session, task.source_id)
            except ConnectorFailure:
                return None
            active = session.scalar(
                select(func.count()).select_from(CollectionJob).where(
                    CollectionJob.status.in_(("pending", "running"))
                )
            ) or 0
            if active >= 20:
                return None
            job = CollectionJob(
                job_id=str(uuid4()),
                command_hash=command_hash,
                request_hash=request_hash,
                task_id=task.id,
                task_definition_hash=task.definition_hash(),
                source_id=task.source_id,
                run_id=str(uuid4()),
                status="pending",
                actor=actor,
                result_summary={},
                created_at=now,
            )
            session.add(job)
            session.add(AuditEvent(
                actor=actor,
                action="scheduled_collection_job_queued",
                target=task.id,
                detail={
                    "job_id": job.job_id,
                    "schedule_id": schedule.id,
                    "scheduled_for": _iso(slot),
                    "task_definition_hash": job.task_definition_hash,
                },
            ))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
            return job.job_id

    def _mark_queue_failure(self, job_id: str) -> None:
        with Session(self.engine) as session:
            job = session.get(CollectionJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_code = "queue_unavailable"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()

    @staticmethod
    def _current_slot(schedule: SnapshotSchedule, now: datetime) -> datetime:
        if now < schedule.anchor_utc:
            return schedule.anchor_utc
        interval = timedelta(minutes=schedule.interval_minutes)
        return schedule.anchor_utc + interval * ((now - schedule.anchor_utc) // interval)

    @staticmethod
    def _actor(schedule_id: str) -> str:
        return f"snapshot-scheduler:{schedule_id}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("schedule times must include a timezone")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
