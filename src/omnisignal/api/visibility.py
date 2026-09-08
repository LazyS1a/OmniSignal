"""Bounded read-only views over video-search snapshots, never arbitrary raw payloads."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from omnisignal.storage import IngestedRecord, IngestionRun
from omnisignal.measurement import ObservationContext

router = APIRouter(prefix="/ops/visibility", tags=["operations"])
SOURCE = "youtube_visibility"


class MatchView(BaseModel):
    brand_id: str = Field(max_length=40)
    aliases: list[str] = Field(max_length=20)
    origin: Literal["official", "third_party", "unverified"]
    official_channel_match: bool

    @field_validator("aliases")
    @classmethod
    def bounded_aliases(cls, values):
        if any(len(v) > 100 for v in values):
            raise ValueError("alias too long")
        return values


class EvidenceView(BaseModel):
    position: int = Field(ge=1, le=50, strict=True)
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    title: str = Field(min_length=1, max_length=2000)
    matches: list[MatchView] = Field(max_length=10)


class BrandView(BaseModel):
    brand_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    role: Literal["owned", "competitor"]
    count: int = Field(ge=0, le=50, strict=True)
    denominator: int = Field(ge=0, le=50, strict=True)
    sample_share_percent: float | None = Field(ge=0, le=100)
    first_position: int | None = Field(ge=1, le=50)
    official_count: int = Field(ge=0, le=50, strict=True)
    third_party_title_count: int = Field(ge=0, le=50, strict=True)
    unverified_origin_count: int = Field(ge=0, le=50, strict=True)


class SnapshotView(BaseModel):
    metric_type: Literal["video_search_sample_visibility"]
    query: str = Field(min_length=1, max_length=120)
    platform: Literal["youtube"]
    scope: Literal["yt_dlp_video_search_order"]
    requested_language: str = Field(max_length=20)
    is_example: bool
    requested_top_k: int = Field(ge=1, le=50, strict=True)
    valid_result_count: int = Field(ge=0, le=50, strict=True)
    quality_status: Literal["complete", "incomplete"]
    fetched_at: datetime
    collector_version: str = Field(max_length=100)
    rule_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    calculation_version: Literal["1.0"]
    warnings: list[Literal["extractor_warning", "invalid_entry"]] = Field(max_length=2)
    brands: list[BrandView] = Field(min_length=1, max_length=10)
    evidence: list[EvidenceView] = Field(max_length=50)
    observation_context: ObservationContext | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.fetched_at.tzinfo is None or len(self.evidence) != self.valid_result_count:
            raise ValueError("invalid snapshot scope")
        if self.valid_result_count > self.requested_top_k:
            raise ValueError("too many results")
        ids = {b.brand_id for b in self.brands}
        if len(ids) != len(self.brands) or sum(b.role == "owned" for b in self.brands) != 1:
            raise ValueError("ambiguous brands")
        if len({e.video_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate videos")
        positions = [e.position for e in self.evidence]
        if positions != sorted(set(positions)) or any(p > self.requested_top_k for p in positions):
            raise ValueError("invalid positions")
        complete = self.quality_status == "complete"
        if complete and (self.valid_result_count != self.requested_top_k or self.warnings):
            raise ValueError("incomplete scope labeled complete")
        for e in self.evidence:
            if len({m.brand_id for m in e.matches}) != len(e.matches) or any(m.brand_id not in ids for m in e.matches):
                raise ValueError("invalid matching evidence")
        for b in self.brands:
            hits = [(e, m) for e in self.evidence for m in e.matches if m.brand_id == b.brand_id]
            expected = round(100 * len(hits) / self.valid_result_count, 4) if complete else None
            if (b.count != len(hits) or b.denominator != self.valid_result_count or b.sample_share_percent != expected
                or b.first_position != (hits[0][0].position if hits else None)
                or b.official_count != sum(m.origin == "official" for _, m in hits)
                or b.third_party_title_count != sum(m.origin == "third_party" for _, m in hits)
                or b.unverified_origin_count != sum(m.origin == "unverified" for _, m in hits)):
                raise ValueError("counters differ from evidence")
        return self


def project(row: IngestedRecord, *, detail: bool) -> dict:
    try:
        report = SnapshotView.model_validate(row.payload["visibility_snapshot"])
    except (KeyError, TypeError, ValidationError):
        return {"snapshot_id": row.source_record_id, "validation_status": "invalid_snapshot"}
    document = report.model_dump(mode="json", exclude=set() if detail else {"evidence", "brands", "warnings"})
    age = max(0, int((datetime.now(timezone.utc) - report.fetched_at).total_seconds()))
    if detail:
        for e in document["evidence"]:
            e["url"] = f"https://www.youtube.com/watch?v={e['video_id']}"
    return {"snapshot_id": row.source_record_id, "validation_status": "valid", "age_seconds": age,
            "older_than_24h": age > 86400, "raw_archive_sha256": row.raw_archive_sha256, **document}


@router.get("")
def list_snapshots(request: Request, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0, le=100000),
                   q: str = Query("", max_length=120)) -> dict:
    statement = select(IngestedRecord).where(IngestedRecord.source_id == SOURCE, IngestedRecord.deleted_at.is_(None))
    if q:
        statement = statement.where(IngestedRecord.payload["visibility_snapshot"]["query"].as_string().contains(q, autoescape=True))
    with Session(request.app.state.engine) as session:
        total = session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = session.scalars(statement.order_by(IngestedRecord.first_seen_at.desc(), IngestedRecord.source_record_id.desc())
                                .offset(offset).limit(limit)).all()
        latest = session.scalar(select(IngestionRun).where(IngestionRun.source_id == SOURCE)
                                .order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc()).limit(1))
        return {"total": total, "limit": limit, "offset": offset, "items": [project(r, detail=False) for r in rows],
                "latest_source_run": {"status": latest.status.value, "finished_at": latest.finished_at} if latest else None}


@router.get("/{snapshot_id}")
def snapshot_detail(request: Request, snapshot_id: str) -> dict:
    if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
        raise HTTPException(404, "snapshot not found")
    with Session(request.app.state.engine) as session:
        row = session.get(IngestedRecord, (SOURCE, snapshot_id))
        if row is None or row.deleted_at is not None:
            raise HTTPException(404, "snapshot not found")
        result = project(row, detail=True)
        if result["validation_status"] != "valid":
            raise HTTPException(409, "snapshot schema or counters are invalid")
        return result
