"""Isolated worker for Google Trends relative interest and public suggestions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .public_search_protocol import COLLECTOR_VERSION, OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION


TRENDS_URL = "https://trends.google.com/trends/"
SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
_INPUT_FIELDS = {
    "protocol_version",
    "keywords",
    "geo",
    "timeframe",
    "search_property",
    "language",
    "timezone_minutes",
    "include_suggestions",
    "max_suggestions",
    "limit",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _identity(*parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_job(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INPUT_FIELDS:
        raise ValueError("job envelope fields changed")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("job protocol version changed")
    keywords = value.get("keywords")
    if (
        not isinstance(keywords, list)
        or not 1 <= len(keywords) <= 5
        or any(not isinstance(item, str) or not item or len(item) > 100 for item in keywords)
    ):
        raise ValueError("job keywords are invalid")
    if value.get("search_property") not in {"web", "youtube"}:
        raise ValueError("job search property is invalid")
    limit = value.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
        raise ValueError("job limit is invalid")
    return value


def _trend_records(job: dict[str, Any]) -> list[dict[str, Any]]:
    from pytrends.request import TrendReq

    keywords = list(job["keywords"])
    property_name = str(job["search_property"])
    client = TrendReq(
        hl=str(job["language"]),
        tz=int(job["timezone_minutes"]),
        retries=0,
        backoff_factor=0,
        timeout=(5, 10),
    )
    client.build_payload(
        keywords,
        cat=0,
        timeframe=str(job["timeframe"]),
        geo=str(job["geo"]),
        gprop="" if property_name == "web" else property_name,
    )
    frame = client.interest_over_time()
    if frame.empty:
        return []
    if any(keyword not in frame.columns for keyword in keywords) or "isPartial" not in frame.columns:
        raise ValueError("Google Trends response columns changed")
    comparison_group = _identity("comparison", property_name, job["geo"], job["timeframe"], sorted(keywords), job["language"], job["timezone_minutes"], [stamp.isoformat() for stamp in frame.index])
    records: list[dict[str, Any]] = []
    for observed, row in frame.iterrows():
        observed_at = observed.isoformat()
        partial = bool(row["isPartial"])
        for keyword in keywords:
            value = int(row[keyword])
            records.append(
                {
                    "source_record_id": _identity(
                        "relative_interest", comparison_group, keyword.casefold(), observed_at
                    ),
                    "query": keyword,
                    "related_query": None,
                    "platform": property_name,
                    "metric_type": "relative_interest",
                    "value": value,
                    "unit": "index_0_100",
                    "geo": str(job["geo"]),
                    "time_window": str(job["timeframe"]),
                    "observed_at": observed_at,
                    "is_partial": partial,
                    "scope": "google_trends_same_request_scale",
                    "comparison_group": comparison_group,
                    "source_url": TRENDS_URL,
                    "collector_version": COLLECTOR_VERSION,
                    "quality_status": "partial_period" if partial else "accepted",
                }
            )
    return records


def _suggestion_records(job: dict[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    if not job["include_suggestions"]:
        return []
    import requests

    property_name = str(job["search_property"])
    observed_on = fetched_at.date().isoformat()
    records: list[dict[str, Any]] = []
    session = requests.Session()
    session.headers["User-Agent"] = "OmniSignal/0.1 public-search-signals"
    try:
        for keyword in job["keywords"]:
            params = {"client": "firefox", "q": keyword, "hl": str(job["language"])}
            if property_name == "youtube":
                params["ds"] = "yt"
            response = session.get(SUGGEST_URL, params=params, timeout=10)
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, list) or len(document) < 2 or not isinstance(document[1], list):
                raise ValueError("Google Suggest response schema changed")
            suggestions = [item for item in document[1] if isinstance(item, str) and item.strip()]
            for rank, suggestion in enumerate(suggestions[: int(job["max_suggestions"])], start=1):
                records.append(
                    {
                        "source_record_id": _identity(
                            "suggestion_rank",
                            property_name,
                            str(job["geo"]),
                            keyword.casefold(),
                            observed_on,
                            suggestion.casefold(),
                        ),
                        "query": keyword,
                        "related_query": suggestion,
                        "platform": property_name,
                        "metric_type": "suggestion_rank",
                        "value": rank,
                        "unit": "rank_1_best",
                        "geo": "",
                        "time_window": "daily_snapshot",
                        "observed_at": observed_on,
                        "is_partial": False,
                        "scope": "public_autocomplete_snapshot",
                        "comparison_group": None,
                        "source_url": SUGGEST_URL,
                        "collector_version": COLLECTOR_VERSION,
                        "quality_status": "accepted",
                    }
                )
    finally:
        session.close()
    return records


def _failure(exc: Exception) -> dict[str, object]:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    if status == 429 or "toomanyrequests" in name:
        kind = "rate_limit"
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        kind = "dependency_missing"
    elif isinstance(exc, ValueError):
        kind = "schema_drift"
    else:
        kind = "upstream"
    return {"kind": kind, "status": status if isinstance(status, int) else None}


def collect(job: dict[str, Any], *, now: datetime | None = None) -> dict[str, object]:
    fetched_at = now or _utc_now()
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    failures: list[dict[str, object]] = []
    try:
        trends = _trend_records(job)
        records.extend(trends)
        if not trends:
            warnings.append("trend_empty")
    except Exception as exc:  # worker converts third-party exceptions to a stable envelope
        failures.append(_failure(exc))
        warnings.append("trend_unavailable")
    try:
        suggestions = _suggestion_records(job, fetched_at)
        records.extend(suggestions)
        if job["include_suggestions"] and not suggestions:
            warnings.append("suggestions_empty")
    except Exception as exc:  # worker converts third-party exceptions to a stable envelope
        failures.append(_failure(exc))
        warnings.append("suggestions_unavailable")

    if not records and failures:
        priority = {"rate_limit": 0, "dependency_missing": 1, "schema_drift": 2, "upstream": 3}
        error = min(failures, key=lambda item: priority.get(str(item.get("kind")), 99))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
            "status": "error",
            "records": [],
            "warnings": sorted(set(warnings)),
            "fetched_at": fetched_at.isoformat(),
            "error": error,
        }
    if len(records) > int(job["limit"]):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
            "status": "error",
            "records": [],
            "warnings": sorted(set(warnings)),
            "fetched_at": fetched_at.isoformat(),
            "error": {"kind": "resource_exhausted", "status": None},
        }
    return {
        "protocol_version": PROTOCOL_VERSION,
        "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
        "status": "ok",
        "records": records,
        "warnings": sorted(set(warnings)),
        "fetched_at": fetched_at.isoformat(),
        "error": None,
    }


def _write_result(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        job = _validate_job(json.loads(args.job.read_text(encoding="utf-8")))
        result = collect(job)
    except (OSError, ValueError):
        result = {
            "protocol_version": PROTOCOL_VERSION,
            "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
            "status": "error",
            "records": [],
            "warnings": [],
            "fetched_at": _utc_now().isoformat(),
            "error": {"kind": "invalid_job", "status": None},
        }
    _write_result(args.result, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
