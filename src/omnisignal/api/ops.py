"""Bounded, read-only operational queries for the local management console."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import yaml

from omnisignal.storage import (
    AuditEvent,
    ConnectorState,
    ContextEdge,
    DuplicateGroup,
    DuplicateMembership,
    IngestedRecord,
    IngestionRun,
    NormalizationRun,
    NormalizationRunMembership,
    NormalizedRecord,
    PluginOutput,
    PluginRun,
    QualityEvent,
    SourceControlState,
)


router = APIRouter(prefix="/ops", tags=["operations"])
_SECRET_MARKERS = ("token", "secret", "password", "cookie", "private_key", "authorization")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_EXPORT_FIELDS = (
    "normalized_id",
    "source_id",
    "source_record_id",
    "source_schema_version",
    "normalizer_version",
    "config_hash",
    "quality_status",
    "normalized_hash",
    "raw_archive_sha256",
    "permission",
    "canonical_url",
    "canonical_url_truncated",
    "title",
    "title_truncated",
    "text",
    "text_truncated",
    "language",
    "published_at",
    "updated_at",
    "created_at",
)


def _session(request: Request) -> Session:
    return Session(request.app.state.engine)


def _count(session: Session, statement) -> int:
    return int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


@router.get("/summary")
def summary(request: Request, window_hours: int = Query(24, ge=1, le=720)) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    review_cutoff = now - timedelta(hours=24)
    with _session(request) as session:
        active_records = int(
            session.scalar(
                select(func.count()).select_from(IngestedRecord).where(IngestedRecord.deleted_at.is_(None))
            )
            or 0
        )
        archived_records = int(
            session.scalar(
                select(func.count())
                .select_from(IngestedRecord)
                .where(
                    IngestedRecord.deleted_at.is_(None),
                    IngestedRecord.raw_archive_sha256.is_not(None),
                )
            )
            or 0
        )
        ingestion_total = int(
            session.scalar(
                select(func.count())
                .select_from(IngestionRun)
                .where(IngestionRun.created_at >= cutoff)
            )
            or 0
        )
        ingestion_failed = int(
            session.scalar(
                select(func.count())
                .select_from(IngestionRun)
                .where(
                    IngestionRun.created_at >= cutoff,
                    IngestionRun.status.in_(["FAILED", "PAUSED", "QUARANTINED"]),
                )
            )
            or 0
        )
        unfinished_statement = select(IngestionRun).where(IngestionRun.status.in_(["PENDING", "RUNNING"]))
        unfinished_total = _count(session, unfinished_statement)
        unfinished_review = _count(session, unfinished_statement.where(IngestionRun.created_at < review_cutoff))
        normalized_versions = int(session.scalar(select(func.count()).select_from(NormalizedRecord)) or 0)
        plugin_attempts = int(
            session.scalar(
                select(func.count()).select_from(PluginRun).where(PluginRun.created_at >= cutoff)
            )
            or 0
        )
        quality_rows = session.execute(
            select(NormalizedRecord.quality_status, func.count())
            .group_by(NormalizedRecord.quality_status)
            .order_by(NormalizedRecord.quality_status)
        ).all()
    return {
        "window": {"hours": window_hours, "starts_at": cutoff, "scope": "created_at"},
        "ingestion_runs": {"total": ingestion_total, "stopped": ingestion_failed},
        "unfinished_ingestion": {
            "total": unfinished_total,
            "needs_review": unfinished_review,
            "review_after_hours": 24,
            "scope": "all_history",
        },
        "records": {"active_ingested": active_records, "normalized_versions": normalized_versions},
        "plugin_attempts": plugin_attempts,
        "quality_statuses": {str(name): int(count) for name, count in quality_rows},
        "archive_coverage": {
            "numerator": archived_records,
            "denominator": active_records,
            "ratio": _ratio(archived_records, active_records),
            "scope": "active_ingested_records",
        },
    }


@router.get("/sources")
def sources(request: Request) -> dict[str, object]:
    registry = _load_registry(request.app.state.registry_path)
    items: list[dict[str, object]] = []
    with _session(request) as session:
        for entry in registry["sources"]:
            source_id = str(entry["id"])
            state = session.get(ConnectorState, source_id)
            control = session.get(SourceControlState, source_id)
            latest = session.scalars(
                select(IngestionRun)
                .where(IngestionRun.source_id == source_id)
                .order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc())
                .limit(1)
            ).first()
            registry_enabled = entry.get("status") == "allowed"
            effective_enabled = registry_enabled and (control.enabled if control else True)
            items.append(
                {
                    "source_id": source_id,
                    "display_name": entry.get("display_name", source_id),
                    "connector_class": entry.get("connector_class"),
                    "registry_status": entry.get("status"),
                    "authorization_basis": entry.get("authorization_basis"),
                    "authorization_expires_at": entry.get("authorization_expires_at"),
                    "rate_budget_rpm": entry.get("rate_budget_rpm"),
                    "kill_switch": entry.get("kill_switch") is True,
                    "effective_enabled": effective_enabled,
                    "control_override": control is not None,
                    "control_version": control.version if control else 0,
                    "control_updated_at": control.updated_at if control else None,
                    "control_updated_by": control.updated_by if control else None,
                    "checkpoint_version": state.checkpoint_version if state else None,
                    "checkpoint_updated_at": state.updated_at if state else None,
                    "latest_run": _ingestion_run(latest) if latest else None,
                }
            )
    return {
        "registry_version": registry["registry_version"],
        "registry_updated_at": registry["updated_at"],
        "total": len(items),
        "items": items,
    }


@router.get("/runs")
def runs(
    request: Request,
    kind: Literal["ingestion", "normalization", "plugin"] = "ingestion",
    source_id: str | None = Query(None, min_length=1, max_length=64),
    run_status: str | None = Query(None, alias="status", min_length=1, max_length=20),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict[str, object]:
    with _session(request) as session:
        if kind == "ingestion":
            statement = select(IngestionRun)
            if source_id:
                statement = statement.where(IngestionRun.source_id == source_id)
            if run_status:
                statement = statement.where(IngestionRun.status == run_status.upper())
            total = _count(session, statement)
            rows = session.scalars(
                statement.order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            items = [{"kind": "ingestion", **_ingestion_run(row)} for row in rows]
        elif kind == "normalization":
            statement = select(NormalizationRun)
            if source_id:
                statement = (
                    statement.join(NormalizationRunMembership)
                    .where(NormalizationRunMembership.source_id == source_id)
                    .distinct()
                )
            if run_status:
                statement = statement.where(NormalizationRun.status == run_status)
            total = _count(session, statement)
            rows = session.scalars(
                statement.order_by(NormalizationRun.created_at.desc(), NormalizationRun.run_id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            items = [{"kind": "normalization", **_normalization_run(row)} for row in rows]
        else:
            statement = select(PluginRun)
            if source_id:
                statement = (
                    statement.join(PluginOutput)
                    .join(NormalizedRecord, PluginOutput.normalized_id == NormalizedRecord.normalized_id)
                    .where(NormalizedRecord.source_id == source_id)
                    .distinct()
                )
            if run_status:
                statement = statement.where(PluginRun.status == run_status)
            total = _count(session, statement)
            rows = session.scalars(
                statement.order_by(PluginRun.created_at.desc(), PluginRun.run_id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            items = [{"kind": "plugin", **_plugin_run(row)} for row in rows]
    return {"kind": kind, "total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/runs/{kind}/{run_id}")
def run_detail(
    request: Request,
    kind: Literal["ingestion", "normalization", "plugin"],
    run_id: str,
) -> dict[str, object]:
    with _session(request) as session:
        if kind == "ingestion":
            row = session.get(IngestionRun, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            audit_count = int(
                session.scalar(
                    select(func.count()).select_from(AuditEvent).where(AuditEvent.run_id == run_id)
                )
                or 0
            )
            return {"kind": kind, **_ingestion_run(row), "audit_event_count": audit_count}
        if kind == "normalization":
            row = session.get(NormalizationRun, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            membership_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(NormalizationRunMembership)
                    .where(NormalizationRunMembership.normalization_run_id == run_id)
                )
                or 0
            )
            return {
                "kind": kind,
                **_normalization_run(row),
                "membership_coverage": {
                    "numerator": membership_count,
                    "denominator": row.input_count,
                    "ratio": _ratio(membership_count, row.input_count),
                    "scope": "normalization_run_inputs",
                },
            }
        row = session.get(PluginRun, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"kind": kind, **_plugin_run(row)}


@router.get("/records")
def records(
    request: Request,
    source_id: str | None = Query(None, min_length=1, max_length=64),
    quality_status: str | None = Query(None, min_length=1, max_length=20),
    q: str | None = Query(None, min_length=1, max_length=200),
    normalization_run_id: str | None = Query(None, min_length=1, max_length=64),
    plugin_run_id: str | None = Query(None, min_length=1, max_length=36),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict[str, object]:
    with _session(request) as session:
        statement = _record_statement(
            source_id=source_id,
            quality_status=quality_status,
            q=q,
            normalization_run_id=normalization_run_id,
            plugin_run_id=plugin_run_id,
        )
        total = _count(session, statement)
        rows = session.scalars(
            statement.order_by(NormalizedRecord.created_at.desc(), NormalizedRecord.normalized_id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        items = [_record_list_item(row) for row in rows]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/exports/records.csv")
def export_records_csv(
    request: Request,
    source_id: str | None = Query(None, min_length=1, max_length=64),
    quality_status: str | None = Query(None, min_length=1, max_length=20),
    q: str | None = Query(None, min_length=1, max_length=200),
    limit: int = Query(1000, ge=1, le=1000),
) -> Response:
    try:
        with _session(request) as session:
            statement = _record_statement(
                source_id=source_id,
                quality_status=quality_status,
                q=q,
            )
            rows = session.scalars(
                statement.order_by(
                    NormalizedRecord.created_at.desc(), NormalizedRecord.normalized_id.desc()
                ).limit(limit)
            ).all()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operations database unavailable",
        ) from exc

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_EXPORT_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_record_export_row(row))
    content = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="omnisignal-records.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-OmniSignal-Export-Rows": str(len(rows)),
            "X-OmniSignal-Export-Limit": str(limit),
        },
    )


@router.get("/records/{normalized_id}")
def record_detail(request: Request, normalized_id: str) -> dict[str, object]:
    with _session(request) as session:
        record = session.get(NormalizedRecord, normalized_id)
        if record is None:
            raise HTTPException(status_code=404, detail="record not found")
        events = session.scalars(
            select(QualityEvent)
            .where(QualityEvent.normalized_id == normalized_id)
            .order_by(QualityEvent.created_at, QualityEvent.event_id)
        ).all()
        memberships = session.scalars(
            select(NormalizationRunMembership)
            .where(NormalizationRunMembership.normalized_id == normalized_id)
            .order_by(NormalizationRunMembership.created_at, NormalizationRunMembership.normalization_run_id)
        ).all()
        duplicate_rows = session.execute(
            select(DuplicateGroup, DuplicateMembership)
            .join(DuplicateMembership, DuplicateGroup.group_id == DuplicateMembership.group_id)
            .where(DuplicateMembership.normalized_id == normalized_id)
            .order_by(DuplicateGroup.created_at, DuplicateGroup.group_id)
        ).all()
        contexts = session.scalars(
            select(ContextEdge)
            .where(
                or_(
                    ContextEdge.from_normalized_id == normalized_id,
                    ContextEdge.to_normalized_id == normalized_id,
                )
            )
            .order_by(ContextEdge.created_at, ContextEdge.edge_id)
        ).all()
        plugin_rows = session.execute(
            select(PluginOutput, PluginRun)
            .join(PluginRun, PluginOutput.run_id == PluginRun.run_id)
            .where(PluginOutput.normalized_id == normalized_id)
            .order_by(PluginRun.created_at.desc(), PluginRun.run_id.desc())
        ).all()
        return {
            **_record_detail(record),
            "quality_events": [
                {
                    "event_id": event.event_id,
                    "normalization_run_id": event.normalization_run_id,
                    "code": event.code,
                    "severity": event.severity,
                    "field": event.field,
                    "detail": _redact(event.detail),
                    "created_at": event.created_at,
                }
                for event in events
            ],
            "normalization_runs": [
                {
                    "run_id": member.normalization_run_id,
                    "disposition": member.disposition,
                    "created_at": member.created_at,
                }
                for member in memberships
            ],
            "duplicate_groups": [
                {
                    "group_id": group.group_id,
                    "kind": group.duplicate_kind,
                    "member_count": group.member_count,
                    "normalization_run_id": group.normalization_run_id,
                }
                for group, _ in duplicate_rows
            ],
            "context_edges": [
                {
                    "edge_id": edge.edge_id,
                    "from_normalized_id": edge.from_normalized_id,
                    "to_normalized_id": edge.to_normalized_id,
                    "relation": edge.relation,
                    "normalization_run_id": edge.normalization_run_id,
                }
                for edge in contexts
            ],
            "plugin_outputs": [
                {
                    "run_id": plugin_run.run_id,
                    "plugin_id": plugin_run.plugin_id,
                    "plugin_version": plugin_run.plugin_version,
                    "output_schema_version": plugin_run.output_schema_version,
                    "values": _redact(output.values),
                    "quality_status": output.quality_status,
                    "quality_codes": output.quality_codes,
                    "created_at": output.created_at,
                }
                for output, plugin_run in plugin_rows
            ],
        }


@router.get("/quality")
def quality(request: Request) -> dict[str, object]:
    with _session(request) as session:
        denominator = int(session.scalar(select(func.count()).select_from(NormalizedRecord)) or 0)
        statuses = session.execute(
            select(NormalizedRecord.quality_status, func.count())
            .group_by(NormalizedRecord.quality_status)
            .order_by(NormalizedRecord.quality_status)
        ).all()
        codes = session.execute(
            select(QualityEvent.code, QualityEvent.severity, func.count())
            .group_by(QualityEvent.code, QualityEvent.severity)
            .order_by(QualityEvent.severity, QualityEvent.code)
        ).all()
    return {
        "quality_statuses": [
            {
                "status": name,
                "numerator": int(count),
                "denominator": denominator,
                "ratio": _ratio(int(count), denominator),
                "scope": "all_normalized_versions",
            }
            for name, count in statuses
        ],
        "event_codes": [
            {"code": code, "severity": severity, "count": int(count), "scope": "quality_events"}
            for code, severity, count in codes
        ],
    }


@router.get("/audit")
def audit(
    request: Request,
    action: str | None = Query(None, min_length=1, max_length=80),
    target: str | None = Query(None, min_length=1, max_length=256),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict[str, object]:
    with _session(request) as session:
        statement = select(AuditEvent)
        if action:
            statement = statement.where(AuditEvent.action == action)
        if target:
            statement = statement.where(AuditEvent.target == target)
        total = _count(session, statement)
        rows = session.scalars(
            statement.order_by(AuditEvent.created_at.desc(), AuditEvent.event_id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        items = [
            {
                "event_id": row.event_id,
                "run_id": row.run_id,
                "actor": row.actor,
                "action": row.action,
                "target": row.target,
                "detail": _redact(row.detail),
                "created_at": row.created_at,
            }
            for row in rows
        ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _load_registry(path: Path) -> dict[str, object]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="source registry unavailable",
        ) from exc
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("registry_version"), int)
        or not isinstance(document.get("updated_at"), str)
        or not isinstance(document.get("sources"), list)
        or any(not isinstance(entry, dict) or not isinstance(entry.get("id"), str) for entry in document["sources"])
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="source registry unavailable",
        )
    return document


def _ingestion_run(row: IngestionRun) -> dict[str, object]:
    return {
        "run_id": row.run_id,
        "source_id": row.source_id,
        "status": _status_value(row.status),
        "records_seen": row.records_seen,
        "records_written": row.records_written,
        "error_code": row.error_code,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }


def _normalization_run(row: NormalizationRun) -> dict[str, object]:
    return {
        "run_id": row.run_id,
        "status": row.status,
        "config_version": row.config_version,
        "config_hash": row.config_hash,
        "normalizer_version": row.normalizer_version,
        "input_set_hash": row.input_set_hash,
        "input_count": row.input_count,
        "normalized_count": row.normalized_count,
        "unconfigured_count": row.unconfigured_count,
        "warning_count": row.warning_count,
        "quarantined_count": row.quarantined_count,
        "duplicate_group_count": row.duplicate_group_count,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


def _plugin_run(row: PluginRun) -> dict[str, object]:
    return {
        "run_id": row.run_id,
        "execution_id": row.execution_id,
        "plugin_id": row.plugin_id,
        "plugin_version": row.plugin_version,
        "manifest_hash": row.manifest_hash,
        "output_schema_version": row.output_schema_version,
        "status": row.status,
        "records_seen": row.records_seen,
        "records_sent": row.records_sent,
        "records_skipped": row.records_skipped,
        "output_count": row.output_count,
        "output_hash": row.output_hash,
        "error_code": row.error_code,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
    }


def _record_statement(
    *,
    source_id: str | None = None,
    quality_status: str | None = None,
    q: str | None = None,
    normalization_run_id: str | None = None,
    plugin_run_id: str | None = None,
):
    statement = select(NormalizedRecord)
    if normalization_run_id:
        statement = statement.join(NormalizationRunMembership).where(
            NormalizationRunMembership.normalization_run_id == normalization_run_id
        )
    if plugin_run_id:
        statement = statement.join(PluginOutput).where(PluginOutput.run_id == plugin_run_id)
    if source_id:
        statement = statement.where(NormalizedRecord.source_id == source_id)
    if quality_status:
        statement = statement.where(NormalizedRecord.quality_status == quality_status)
    if q:
        term = q.strip()
        statement = statement.where(
            or_(
                NormalizedRecord.title.contains(term, autoescape=True),
                NormalizedRecord.text.contains(term, autoescape=True),
            )
        )
    return statement.distinct()


def _record_export_row(row: NormalizedRecord) -> dict[str, object]:
    canonical_url, canonical_url_truncated = _export_cell(row.canonical_url, max_chars=2048)
    title, title_truncated = _export_cell(row.title, max_chars=500)
    text, text_truncated = _export_cell(row.text, max_chars=4000)
    values = {
        "normalized_id": row.normalized_id,
        "source_id": row.source_id,
        "source_record_id": row.source_record_id,
        "source_schema_version": row.source_schema_version,
        "normalizer_version": row.normalizer_version,
        "config_hash": row.config_hash,
        "quality_status": row.quality_status,
        "normalized_hash": row.normalized_hash,
        "raw_archive_sha256": row.raw_archive_sha256,
        "permission": row.permission,
        "canonical_url": canonical_url,
        "canonical_url_truncated": str(canonical_url_truncated).lower(),
        "title": title,
        "title_truncated": str(title_truncated).lower(),
        "text": text,
        "text_truncated": str(text_truncated).lower(),
        "language": row.language,
        "published_at": row.published_at,
        "updated_at": row.updated_at,
        "created_at": row.created_at,
    }
    safe: dict[str, object] = {}
    for key, value in values.items():
        if key in {
            "canonical_url",
            "canonical_url_truncated",
            "title",
            "text",
            "title_truncated",
            "text_truncated",
        }:
            safe[key] = value
        else:
            safe[key] = _export_cell(value)[0]
    return safe


def _export_cell(value: object, *, max_chars: int | None = None) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, datetime):
        text = value.isoformat()
    else:
        text = str(value)
    truncated = max_chars is not None and len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    if text.lstrip().startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text, truncated


def _record_list_item(row: NormalizedRecord) -> dict[str, object]:
    text = row.text or ""
    return {
        "normalized_id": row.normalized_id,
        "source_id": row.source_id,
        "source_record_id": row.source_record_id,
        "canonical_url": row.canonical_url,
        "title": row.title,
        "text_preview": text[:240] or None,
        "text_length": len(text),
        "published_at": row.published_at,
        "language": row.language,
        "entity_ids": row.entity_ids,
        "quality_status": row.quality_status,
        "quality_codes": row.quality_codes,
        "raw_archive_sha256": row.raw_archive_sha256,
        "normalizer_version": row.normalizer_version,
        "created_at": row.created_at,
    }


def _record_detail(row: NormalizedRecord) -> dict[str, object]:
    return {
        **_record_list_item(row),
        "text": row.text,
        "input_raw_hash": row.input_raw_hash,
        "permission": row.permission,
        "source_schema_version": row.source_schema_version,
        "config_hash": row.config_hash,
        "normalized_hash": row.normalized_hash,
        "updated_at": row.updated_at,
        "exact_fingerprint": row.exact_fingerprint,
        "simhash64": row.simhash64,
        "parent_source_record_id": row.parent_source_record_id,
    }


def _redact(value: object, *, key: str | None = None) -> object:
    if key is not None and any(marker in key.casefold() for marker in _SECRET_MARKERS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str) and (_WINDOWS_PATH.match(value) or value.startswith("/")):
        return "[redacted_path]"
    return value
