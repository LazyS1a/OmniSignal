"""Read and trigger fixed allowlisted collection tasks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnisignal.contracts import ConnectorFailure
from omnisignal.storage import AuditEvent, CollectionJob, assert_source_enabled
from omnisignal.search_profiles import SearchProfile

from .auth import ControlPrincipal, require_operator
from .ops import _load_registry


router = APIRouter(prefix="/ops", tags=["operations-collection"])
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}")


@router.get("/search-profiles")
def list_search_profiles(request: Request) -> dict[str, object]:
    return request.app.state.search_profiles.list()


@router.post("/search-profiles")
def save_search_profile(body: SearchProfile, request: Request,
                        principal: ControlPrincipal = Depends(require_operator)) -> dict[str, object]:
    try:
        result = request.app.state.search_profiles.save(body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="无法保存配置，请检查配置数量或本地配置文件。") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="配置存储不可写，请检查磁盘空间与权限。") from exc
    with Session(request.app.state.engine) as session:
        session.add(AuditEvent(actor=principal.actor, action="search_profile_saved", target=result["task_id"],
                               detail={"replayed": result["replayed"]}))
        session.commit()
    return result


class CollectionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
    confirmation: str = Field(min_length=1, max_length=96)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _job_payload(job: CollectionJob, *, replayed: bool = False) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "task_id": job.task_id,
        "source_id": job.source_id,
        "run_id": job.run_id,
        "status": job.status,
        "actor": job.actor,
        "result_summary": job.result_summary or {},
        "error_code": job.error_code,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "replayed": replayed,
    }


@router.get("/collection-tasks")
def list_collection_tasks(request: Request) -> dict[str, object]:
    tasks = []
    for task in request.app.state.collection_catalog.tasks.values():
        readiness = request.app.state.collection_readiness.check(task).as_dict()
        tasks.append({
            "task_id": task.id,
            "display_name": task.display_name,
            "description": task.description,
            "source_id": task.source_id,
            "keyword_set": task.keyword_set.model_dump(mode="json"),
            "entity_set": task.entity_set.model_dump(mode="json") if task.entity_set else None,
            "access_tier": task.access_tier,
            "observation_scope": task.observation_scope,
            "is_example": task.is_example,
            "enabled": task.enabled,
            "readiness": readiness,
        })
    return {"items": tasks, "count": len(tasks)}


@router.get("/snapshot-schedules")
def list_snapshot_schedules(request: Request) -> dict[str, object]:
    catalog = request.app.state.snapshot_schedule_catalog
    now = datetime.now(timezone.utc)
    items = []
    for schedule in catalog.schedules.values():
        next_preview = schedule.anchor_utc if now < schedule.anchor_utc else schedule.next_after(now)
        items.append({
            "schedule_id": schedule.id,
            "display_name": schedule.display_name,
            "task_id": schedule.task_id,
            "status": schedule.status,
            "interval_minutes": schedule.interval_minutes,
            "anchor_utc": _iso(schedule.anchor_utc),
            "next_window_preview": _iso(next_preview),
            "eligible_to_trigger": catalog.is_due(schedule, now=now, last_triggered_at=None),
        })
    scheduler_state = request.app.state.snapshot_scheduler.state
    return {
        "auto_start": catalog.auto_start,
        "runtime_state": scheduler_state,
        "items": items,
        "count": len(items),
    }


@router.get("/collection-jobs")
def list_collection_jobs(request: Request, limit: int = Query(20, ge=1, le=100)) -> dict[str, object]:
    with Session(request.app.state.engine) as session:
        jobs = session.scalars(select(CollectionJob).order_by(CollectionJob.created_at.desc()).limit(limit)).all()
        return {"items": [_job_payload(job) for job in jobs], "count": len(jobs)}


@router.get("/collection-jobs/{job_id}")
def get_collection_job(job_id: str, request: Request) -> dict[str, object]:
    if len(job_id) != 36:
        raise HTTPException(status_code=404, detail="collection job not found")
    with Session(request.app.state.engine) as session:
        job = session.get(CollectionJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="collection job not found")
        return _job_payload(job)


@router.post("/collection-jobs", status_code=status.HTTP_202_ACCEPTED)
def start_collection_job(
    body: CollectionStartRequest,
    request: Request,
    principal: ControlPrincipal = Depends(require_operator),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> dict[str, object]:
    if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise HTTPException(status_code=400, detail="invalid idempotency key")
    task = request.app.state.collection_catalog.tasks.get(body.task_id)
    if task is None or not task.enabled:
        raise HTTPException(status_code=404, detail="collection task not found")
    if body.confirmation != f"RUN {task.id}":
        raise HTTPException(status_code=400, detail="confirmation phrase does not match the task")

    registry = _load_registry(request.app.state.registry_path)
    matches = [item for item in registry["sources"] if item.get("id") == task.source_id]
    if len(matches) != 1 or matches[0].get("status") != "allowed":
        raise HTTPException(status_code=409, detail="source registry does not permit collection")
    command_hash = _sha256(f"{principal.actor}\0{idempotency_key}")
    request_hash = _sha256(json.dumps({"actor": principal.actor, **body.model_dump()},
                                     ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    now = datetime.now(timezone.utc)
    with Session(request.app.state.engine) as session:
        previous = session.scalar(select(CollectionJob).where(CollectionJob.command_hash == command_hash))
        if previous is not None:
            if previous.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="idempotency key was already used for a different request")
            return _job_payload(previous, replayed=True)

        readiness = request.app.state.collection_readiness.check(task, force=True)
        if not readiness.available:
            raise HTTPException(
                status_code=503,
                detail={"code": readiness.detail_code, "message": readiness.message},
            )

        try:
            assert_source_enabled(session, task.source_id)
        except ConnectorFailure as exc:
            raise HTTPException(status_code=409, detail=f"source cannot run: {exc.category.value}") from exc
        active = session.scalar(select(func.count()).select_from(CollectionJob).where(
            CollectionJob.status.in_(("pending", "running")))) or 0
        if active >= 20:
            raise HTTPException(status_code=429, detail="collection queue is full")

        from uuid import uuid4

        job = CollectionJob(
            job_id=str(uuid4()),
            command_hash=command_hash,
            request_hash=request_hash,
            task_id=task.id,
            task_definition_hash=task.definition_hash(),
            source_id=task.source_id,
            run_id=str(uuid4()),
            status="pending",
            actor=principal.actor,
            result_summary={},
            created_at=now,
        )
        session.add(job)
        session.add(AuditEvent(actor=principal.actor, action="collection_job_queued", target=task.id,
                               detail={"job_id": job.job_id, "source_id": task.source_id,
                                       "task_definition_hash": job.task_definition_hash,
                                       "idempotency_key_sha256": _sha256(idempotency_key)}))
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="collection job was queued concurrently; refresh") from exc
        job_id = job.job_id
        queued_payload = _job_payload(job)

    try:
        request.app.state.collection_executor.submit(job_id)
    except RuntimeError as exc:
        with Session(request.app.state.engine) as session:
            failed = session.get(CollectionJob, job_id)
            if failed is not None:
                failed.status = "failed"
                failed.error_code = "queue_unavailable"
                failed.finished_at = datetime.now(timezone.utc)
                session.commit()
        raise HTTPException(status_code=503, detail="collection queue is unavailable") from exc
    return queued_payload


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
