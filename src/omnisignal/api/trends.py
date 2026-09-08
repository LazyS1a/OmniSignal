"""Bounded trend series over stored public signals and video-search snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnisignal.measurement import ObservationContext
from omnisignal.storage import IngestedRecord

from .visibility import SnapshotView


router = APIRouter(prefix="/ops/trends", tags=["operations"])
PUBLIC_SOURCE = "public_search_signals"
YOUTUBE_SOURCE = "youtube_visibility"
MAX_SCANNED_ROWS = 5_000
MetricFilter = Literal[
    "all",
    "relative_interest",
    "suggestion_rank",
    "video_result_count",
    "video_sample_share",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object, *, assume_utc_for_naive_bucket: bool = False) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not assume_utc_for_naive_bucket:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _context(value: object, *, fallback_scope: str) -> dict[str, object]:
    try:
        parsed = ObservationContext.model_validate(value)
    except (TypeError, ValidationError):
        return {
            "keyword_set": None,
            "entity_set": None,
            "access_tier": "anonymous_public",
            "scope": fallback_scope,
            "definition_hash": None,
            "provenance_status": "legacy_record_without_versioned_context",
        }
    return {
        **parsed.model_dump(mode="json"),
        "provenance_status": "versioned",
    }


def _series_key(parts: tuple[object, ...]) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _ensure_series(
    series: dict[tuple[object, ...], dict[str, Any]],
    key: tuple[object, ...],
    *,
    source_id: str,
    platform: str,
    query: str,
    subject: str | None,
    metric_type: str,
    unit: str,
    scope: str,
    context: dict[str, object],
    coverage: dict[str, object],
) -> dict[str, Any]:
    if key not in series:
        series[key] = {
            "series_id": _series_key(key),
            "source_id": source_id,
            "platform": platform,
            "query": query,
            "subject": subject,
            "metric_type": metric_type,
            "unit": unit,
            "scope": scope,
            "observation_context": context,
            "coverage": coverage,
            "points": {},
        }
    return series[key]


def _add_public(
    series: dict[tuple[object, ...], dict[str, Any]],
    row: IngestedRecord,
    *,
    cutoff: datetime,
    query_filter: str,
    metric_filter: MetricFilter,
) -> None:
    payload = row.payload
    query = payload.get("query")
    metric = payload.get("metric_type")
    value = payload.get("value")
    observed = _parse_time(payload.get("observed_at"), assume_utc_for_naive_bucket=True)
    if (
        not isinstance(query, str)
        or metric not in {"relative_interest", "suggestion_rank"}
        or not isinstance(value, int)
        or isinstance(value, bool)
        or observed is None
        or observed < cutoff
        or (metric_filter != "all" and metric != metric_filter)
    ):
        return
    related = payload.get("related_query") if metric == "suggestion_rank" else None
    subject = related if isinstance(related, str) else None
    if query_filter and query_filter not in f"{query} {subject or ''}".casefold():
        return
    scope = str(payload.get("scope") or "unknown_public_search_scope")
    context = _context(payload.get("observation_context"), fallback_scope=scope)
    unit = "index_0_100" if metric == "relative_interest" else "rank_1_best"
    geo = payload.get("geo") if isinstance(payload.get("geo"), str) else None
    key = (PUBLIC_SOURCE, metric, query, subject, context.get("definition_hash"), payload.get("comparison_group"))
    target = _ensure_series(
        series,
        key,
        source_id=PUBLIC_SOURCE,
        platform=str(payload.get("platform") or "unknown"),
        query=query,
        subject=subject,
        metric_type=str(metric),
        unit=unit,
        scope=scope,
        context=context,
        coverage={
            "geo": geo,
            "time_window": payload.get("time_window"),
            "time_basis": "source_daily_bucket_rendered_at_utc_midnight",
            "denominator_definition": None,
            "absolute_search_volume": False,
        },
    )
    stamp = _iso(observed)
    target["points"].setdefault(stamp, {
        "observed_at": stamp,
        "value": value,
        "numerator": None,
        "denominator": None,
        "quality_status": payload.get("quality_status"),
        "is_partial": payload.get("is_partial") is True,
    })


def _add_youtube(
    series: dict[tuple[object, ...], dict[str, Any]],
    row: IngestedRecord,
    *,
    cutoff: datetime,
    query_filter: str,
    metric_filter: MetricFilter,
) -> None:
    try:
        snapshot = SnapshotView.model_validate(row.payload.get("visibility_snapshot"))
    except (TypeError, ValidationError):
        return
    observed = snapshot.fetched_at.astimezone(timezone.utc)
    if observed < cutoff:
        return
    context = _context(snapshot.observation_context, fallback_scope=snapshot.scope)
    coverage = {
        "geo": None,
        "requested_language": snapshot.requested_language,
        "requested_top_k": snapshot.requested_top_k,
        "valid_result_count": snapshot.valid_result_count,
        "denominator_definition": "valid unique videos in this extracted sample",
        "absolute_search_volume": False,
    }
    stamp = _iso(observed)
    for brand in snapshot.brands:
        if query_filter and query_filter not in f"{snapshot.query} {brand.name}".casefold():
            continue
        definitions = [
            ("video_result_count", "count", float(brand.count)),
            ("video_sample_share", "percent_0_100", brand.sample_share_percent),
        ]
        for metric, unit, value in definitions:
            if value is None or (metric_filter != "all" and metric != metric_filter):
                continue
            key = (YOUTUBE_SOURCE, metric, snapshot.query, brand.brand_id, context.get("definition_hash"))
            target = _ensure_series(
                series,
                key,
                source_id=YOUTUBE_SOURCE,
                platform="youtube",
                query=snapshot.query,
                subject=brand.name,
                metric_type=metric,
                unit=unit,
                scope=snapshot.scope,
                context=context,
                coverage=coverage,
            )
            target["points"].setdefault(stamp, {
                "observed_at": stamp,
                "value": value,
                "numerator": brand.count,
                "denominator": brand.denominator,
                "quality_status": snapshot.quality_status,
                "is_partial": snapshot.quality_status != "complete",
            })


@router.get("")
def list_trends(
    request: Request,
    days: int = Query(90, ge=1, le=365),
    metric_type: MetricFilter = Query("all"),
    q: str = Query("", max_length=120),
) -> dict[str, object]:
    generated_at = _utc_now()
    cutoff = generated_at - timedelta(days=days)
    query_filter = q.strip().casefold()
    statement = (
        select(IngestedRecord)
        .where(
            IngestedRecord.source_id.in_((PUBLIC_SOURCE, YOUTUBE_SOURCE)),
            IngestedRecord.deleted_at.is_(None),
        )
        .order_by(IngestedRecord.first_seen_at.desc(), IngestedRecord.source_record_id.desc())
        .limit(MAX_SCANNED_ROWS + 1)
    )
    with Session(request.app.state.engine) as session:
        rows = session.scalars(statement).all()
    truncated = len(rows) > MAX_SCANNED_ROWS
    series: dict[tuple[object, ...], dict[str, Any]] = {}
    for row in rows[:MAX_SCANNED_ROWS]:
        if row.source_id == PUBLIC_SOURCE:
            _add_public(series, row, cutoff=cutoff, query_filter=query_filter, metric_filter=metric_type)
        elif row.source_id == YOUTUBE_SOURCE:
            _add_youtube(series, row, cutoff=cutoff, query_filter=query_filter, metric_filter=metric_type)

    items = []
    for item in series.values():
        points = sorted(item.pop("points").values(), key=lambda point: point["observed_at"])
        distinct_days = len({point["observed_at"][:10] for point in points})
        items.append({
            **item,
            "sample_count": len(points),
            "distinct_days": distinct_days,
            "trend_ready": distinct_days >= 2,
            "points": points,
        })
    items.sort(key=lambda item: (item["metric_type"], item["query"].casefold(), str(item["subject"] or "").casefold()))
    return {
        "generated_at": _iso(generated_at),
        "window": {"days": days, "from": _iso(cutoff), "to": _iso(generated_at)},
        "items": items,
        "count": len(items),
        "row_scan_limit": MAX_SCANNED_ROWS,
        "row_scan_truncated": truncated,
        "semantics": {
            "absolute_search_volume_available": False,
            "cross_platform_global_share_available": False,
            "note": "Series preserve source-specific units and scopes; they are not added into a global search share.",
        },
    }
