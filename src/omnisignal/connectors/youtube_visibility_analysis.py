"""Deterministic, evidence-backed counts over one extracted video sample."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .youtube_visibility_policy import YouTubeVisibilityPolicy, normalized


class VideoEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=1, le=50, strict=True)
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    title: str = Field(min_length=1, max_length=2000)
    channel_id: str | None = Field(default=None, pattern=r"^UC[A-Za-z0-9_-]{22}$")


class SearchSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["1.0"] = "1.0"
    query: str
    top_k: int = Field(ge=1, le=50, strict=True)
    fetched_at: datetime
    collector_version: str = Field(min_length=1, max_length=100)
    entries: list[VideoEntry] = Field(max_length=50)
    warnings: list[Literal["extractor_warning", "invalid_entry"]] = Field(default_factory=list, max_length=2)

    @field_validator("fetched_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sample timestamp needs timezone")
        return value


def alias_present(title: str, alias: str) -> bool:
    text, needle = normalized(title), normalized(alias)
    # Latin names need token edges (e.g. 'notion' must not match 'notional').
    left = r"(?<![a-z0-9_])" if needle[0].isascii() and needle[0].isalnum() else ""
    right = r"(?![a-z0-9_])" if needle[-1].isascii() and needle[-1].isalnum() else ""
    return re.search(left + re.escape(needle) + right, text) is not None


def analyze_sample(policy: YouTubeVisibilityPolicy, sample: SearchSample) -> dict:
    if sample.query != policy.query or sample.top_k != policy.top_k or len(sample.entries) > policy.top_k:
        raise ValueError("sample scope differs from query policy")
    positions = [entry.position for entry in sample.entries]
    if positions != sorted(set(positions)) or any(p > policy.top_k for p in positions):
        raise ValueError("sample positions are invalid")
    seen: set[str] = set()
    evidence = []
    duplicate_count = 0
    for entry in sample.entries:
        if entry.video_id in seen:
            duplicate_count += 1
            continue
        seen.add(entry.video_id)
        matches = []
        for brand in policy.brands:
            aliases = [a for a in brand.aliases if alias_present(entry.title, a)]
            official = entry.channel_id in brand.official_channel_ids if entry.channel_id else False
            if aliases or official:
                origin = "official" if official else (
                    "third_party" if brand.official_channel_ids and entry.channel_id else "unverified")
                matches.append({"brand_id": brand.id, "aliases": aliases, "origin": origin,
                                "official_channel_match": official})
        evidence.append({"url": f"https://www.youtube.com/watch?v={entry.video_id}",
                         **entry.model_dump(), "matches": matches})
    denominator = len(evidence)
    complete = denominator == policy.top_k and not sample.warnings and duplicate_count == 0
    brands = []
    for brand in policy.brands:
        hits = [(e, m) for e in evidence for m in e["matches"] if m["brand_id"] == brand.id]
        brands.append({"brand_id": brand.id, "name": brand.name, "role": brand.role,
                       "count": len(hits), "denominator": denominator,
                       "sample_share_percent": round(100 * len(hits) / denominator, 4) if complete else None,
                       "first_position": hits[0][0]["position"] if hits else None,
                       "official_count": sum(m["origin"] == "official" for _, m in hits),
                       "third_party_title_count": sum(m["origin"] == "third_party" for _, m in hits),
                       "unverified_origin_count": sum(m["origin"] == "unverified" for _, m in hits)})
    return {"metric_type": "video_search_sample_visibility", "query": policy.query,
            "platform": "youtube", "scope": "yt_dlp_video_search_order",
            "requested_language": policy.language, "effective_geo": None,
            "personalization": "anonymous_collector", "ad_coverage": "not_measured",
            "title_matching_only": True, "is_example": policy.is_example,
            "requested_top_k": policy.top_k, "valid_result_count": denominator,
            "duplicate_count": duplicate_count, "quality_status": "complete" if complete else "incomplete",
            "fetched_at": sample.fetched_at.isoformat(), "collector_version": sample.collector_version,
            "rule_hash": policy.fingerprint(), "calculation_version": "1.0",
            "warnings": sample.warnings, "brands": brands, "evidence": evidence}
