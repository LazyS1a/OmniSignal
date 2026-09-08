"""Bounded read-only projections over SearXNG result snapshots."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omnisignal.connectors.youtube_visibility_analysis import alias_present
from omnisignal.measurement import ObservationContext
from omnisignal.storage import IngestedRecord, IngestionRun


router = APIRouter(prefix="/ops/web-visibility", tags=["operations"])
SOURCE = "searxng_results"


class SearXNGResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str = Field(min_length=1, max_length=50)
    collector_version: str = Field(min_length=1, max_length=100)
    engine: str = Field(min_length=1, max_length=50)
    language: str = Field(min_length=2, max_length=20)
    observed_at: datetime
    position: int = Field(ge=1, le=50, strict=True)
    published_at: str | None = Field(default=None, max_length=100)
    quality_status: str = Field(pattern=r"^(accepted|incomplete)$")
    query: str = Field(min_length=1, max_length=120)
    sample_complete: bool
    scope: str = Field(min_length=10, max_length=120)
    source_url: str = Field(min_length=10, max_length=500)
    text: str = Field(max_length=4_000)
    time_range: str | None = Field(default=None, max_length=20)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=8, max_length=4_096)
    observation_context: ObservationContext

    @field_validator("observed_at")
    @classmethod
    def aware_observation(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observation time needs timezone")
        return value

    @field_validator("url")
    @classmethod
    def safe_result_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("unsafe result URL")
        return value


def _validated_rows(rows: list[IngestedRecord], request: Request) -> tuple[list[tuple[IngestedRecord, SearXNGResultView]], object]:
    validated: list[tuple[IngestedRecord, SearXNGResultView]] = []
    expected_context = None
    for row in rows:
        report = SearXNGResultView.model_validate(row.payload)
        current_context = report.observation_context
        rebuilt = request.app.state.collection_catalog.measurements.context(
            keyword_ref=current_context.keyword_set,
            entity_ref=current_context.entity_set,
            access_tier=current_context.access_tier,
            scope=current_context.scope,
        )
        if rebuilt.definition_hash != current_context.definition_hash:
            raise ValueError("observation context hash differs from catalog")
        if expected_context is None:
            expected_context = current_context
        elif expected_context != current_context:
            raise ValueError("snapshot contains mixed observation contexts")
        validated.append((row, report))
    if expected_context is None:
        raise ValueError("empty snapshot")
    return validated, expected_context


def _match_entities(report: SearXNGResultView, entities) -> list[dict[str, object]]:
    host = (urlsplit(report.url).hostname or "").casefold()
    searchable = f"{report.title}\n{report.text}"
    matches = []
    for entity in entities:
        aliases = [alias for alias in entity.aliases if alias_present(searchable, alias)]
        official_domain = next(
            (domain for domain in entity.domains if host == domain or host.endswith("." + domain)),
            None,
        )
        if aliases or official_domain:
            matches.append(
                {
                    "entity_id": entity.id,
                    "aliases": aliases,
                    "official_domain": official_domain,
                }
            )
    return matches


def project_snapshot(rows: list[IngestedRecord], request: Request, *, detail: bool) -> dict[str, object]:
    snapshot_id = rows[0].raw_archive_sha256 if rows else None
    try:
        if not snapshot_id or any(row.raw_archive_sha256 != snapshot_id for row in rows):
            raise ValueError("invalid archive identity")
        validated, context = _validated_rows(rows, request)
        keyword_set = request.app.state.collection_catalog.measurements.keyword_set(context.keyword_set)
        entity_set = request.app.state.collection_catalog.measurements.entity_set(context.entity_set)
    except (KeyError, TypeError, ValueError, ValidationError, RuntimeError):
        return {"snapshot_id": snapshot_id or "unknown", "validation_status": "invalid_snapshot"}

    reports = [report for _, report in validated]
    observed_at = max(report.observed_at for report in reports)
    complete = all(report.sample_complete and report.quality_status == "accepted" for report in reports)
    profile_store = getattr(request.app.state, "search_profiles", None)
    profile = profile_store.profiles.get(context.keyword_set.id) if profile_store else None
    observed_slices = {(report.query, report.engine) for report in reports}
    missing_slices = []
    if profile is not None:
        missing_slices = [{"query": query, "engine": engine}
                          for query in profile.queries for engine in profile.engines
                          if (query, engine) not in observed_slices]
        complete = complete and not missing_slices
    base: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "validation_status": "valid",
        "observed_at": observed_at,
        "older_than_24h": (datetime.now(timezone.utc) - observed_at).total_seconds() > 86_400,
        "result_count": len(reports),
        "query_count": len({report.query for report in reports}),
        "engines": sorted({report.engine for report in reports}),
        "quality_status": "complete" if complete else "incomplete",
        "missing_slices": missing_slices,
        "coverage_verified": profile is not None,
        "is_example": keyword_set.is_example,
        "keyword_set": context.keyword_set.model_dump(mode="json"),
        "entity_set": context.entity_set.model_dump(mode="json") if context.entity_set else None,
        "access_tier": context.access_tier,
        "scope": context.scope,
        "raw_archive_sha256": snapshot_id,
    }
    if not detail:
        return base

    grouped: dict[tuple[str, str], list[tuple[IngestedRecord, SearXNGResultView]]] = defaultdict(list)
    for item in validated:
        grouped[(item[1].query, item[1].engine)].append(item)
    slices = []
    entities = entity_set.entities if entity_set else ()
    for (query, engine), items in sorted(grouped.items()):
        items.sort(key=lambda item: (item[1].position, item[0].source_record_id))
        positions = [item[1].position for item in items]
        if positions != sorted(set(positions)):
            return {"snapshot_id": snapshot_id, "validation_status": "invalid_snapshot"}
        evidence = []
        for row, report in items:
            evidence.append(
                {
                    "result_id": row.source_record_id,
                    "position": report.position,
                    "title": report.title,
                    "url": report.url,
                    "text_preview": report.text[:500],
                    "published_at": report.published_at,
                    "quality_status": report.quality_status,
                    "matches": _match_entities(report, entities),
                }
            )
        slice_complete = all(item[1].sample_complete and item[1].quality_status == "accepted" for item in items)
        entity_counts = []
        for entity in entities:
            hits = [item for item in evidence if any(m["entity_id"] == entity.id for m in item["matches"])]
            denominator = len(evidence)
            entity_counts.append(
                {
                    "entity_id": entity.id,
                    "name": entity.name,
                    "role": entity.role,
                    "count": len(hits),
                    "denominator": denominator,
                    "sample_share_percent": (
                        round(100 * len(hits) / denominator, 4) if slice_complete and denominator else None
                    ),
                    "first_position": hits[0]["position"] if hits else None,
                }
            )
        slices.append(
            {
                "query": query,
                "engine": engine,
                "denominator": len(evidence),
                "quality_status": "complete" if slice_complete else "incomplete",
                "entities": entity_counts,
                "results": evidence,
            }
        )
    return {**base, "slices": slices}


@router.get("")
def list_web_snapshots(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=100_000),
    q: str = Query("", max_length=120),
) -> dict[str, object]:
    records_base = select(IngestedRecord).where(
        IngestedRecord.source_id == SOURCE,
        IngestedRecord.deleted_at.is_(None),
        IngestedRecord.raw_archive_sha256.is_not(None),
    )
    filtered = records_base
    if q:
        filtered = filtered.where(IngestedRecord.payload["query"].as_string().contains(q, autoescape=True))
    grouped = filtered.with_only_columns(
        IngestedRecord.raw_archive_sha256.label("snapshot_id"),
        func.max(IngestedRecord.first_seen_at).label("seen_at"),
    ).group_by(IngestedRecord.raw_archive_sha256)
    with Session(request.app.state.engine) as session:
        total = session.scalar(select(func.count()).select_from(grouped.subquery())) or 0
        identities = session.execute(
            grouped.order_by(func.max(IngestedRecord.first_seen_at).desc()).offset(offset).limit(limit)
        ).all()
        snapshot_ids = [row.snapshot_id for row in identities]
        rows = session.scalars(
            records_base.where(IngestedRecord.raw_archive_sha256.in_(snapshot_ids)).order_by(
                IngestedRecord.first_seen_at.desc(), IngestedRecord.source_record_id
            )
        ).all() if snapshot_ids else []
        by_snapshot: dict[str, list[IngestedRecord]] = defaultdict(list)
        for row in rows:
            by_snapshot[str(row.raw_archive_sha256)].append(row)
        latest = session.scalar(
            select(IngestionRun).where(IngestionRun.source_id == SOURCE).order_by(
                IngestionRun.created_at.desc(), IngestionRun.run_id.desc()
            ).limit(1)
        )
        items = [project_snapshot(by_snapshot[snapshot_id], request, detail=False) for snapshot_id in snapshot_ids]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
            "latest_source_run": (
                {"status": latest.status.value, "finished_at": latest.finished_at} if latest else None
            ),
        }


@router.get("/{snapshot_id}")
def web_snapshot_detail(request: Request, snapshot_id: str) -> dict[str, object]:
    if len(snapshot_id) != 64 or any(char not in "0123456789abcdef" for char in snapshot_id):
        raise HTTPException(404, "snapshot not found")
    with Session(request.app.state.engine) as session:
        rows = session.scalars(
            select(IngestedRecord).where(
                IngestedRecord.source_id == SOURCE,
                IngestedRecord.raw_archive_sha256 == snapshot_id,
                IngestedRecord.deleted_at.is_(None),
            ).order_by(IngestedRecord.source_record_id).limit(2_500)
        ).all()
        if not rows:
            raise HTTPException(404, "snapshot not found")
        result = project_snapshot(rows, request, detail=True)
        if result["validation_status"] != "valid":
            raise HTTPException(409, "snapshot schema or context is invalid")
        return result
