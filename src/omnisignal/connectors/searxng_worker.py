"""Isolated, bounded client for a locally hosted SearXNG JSON API."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .searxng_protocol import COLLECTOR_VERSION, OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION


_INPUT_FIELDS = {
    "protocol_version",
    "endpoint",
    "queries",
    "engines",
    "categories",
    "language",
    "time_range",
    "safe_search",
    "max_pages",
    "max_results_per_slice",
    "request_timeout_seconds",
    "limit",
}
_ALLOWED_TIME_RANGES = {None, "day", "month", "year"}
RequestPage = Callable[[str, dict[str, object], int], dict[str, Any]]


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
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str):
        raise ValueError("job endpoint is invalid")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 8888
        or parsed.path != "/search"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("job endpoint left the local allowlist")
    for field, maximum in (("queries", 20), ("engines", 8), ("categories", 5)):
        items = value.get(field)
        if (
            not isinstance(items, list)
            or not 1 <= len(items) <= maximum
            or any(not isinstance(item, str) or not item or len(item) > 120 for item in items)
        ):
            raise ValueError(f"job {field} are invalid")
    if value.get("time_range") not in _ALLOWED_TIME_RANGES:
        raise ValueError("job time range is invalid")
    safe_search = value.get("safe_search")
    if not isinstance(safe_search, int) or isinstance(safe_search, bool) or safe_search not in {0, 1, 2}:
        raise ValueError("job safe-search value is invalid")
    for field, minimum, maximum in (
        ("max_pages", 1, 3),
        ("max_results_per_slice", 1, 50),
        ("request_timeout_seconds", 16, 30),
        ("limit", 1, 10_000),
    ):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or not minimum <= item <= maximum:
            raise ValueError(f"job {field} is invalid")
    if len(value["queries"]) * len(value["engines"]) * int(value["max_pages"]) > 48:
        raise ValueError("job request matrix is too large")
    return value


def _request_page(endpoint: str, params: dict[str, object], timeout_seconds: int) -> dict[str, Any]:
    import requests

    response = requests.get(
        endpoint,
        params=params,
        headers={"Accept": "application/json", "User-Agent": "OmniSignal/0.1 searxng-results"},
        timeout=timeout_seconds,
        allow_redirects=False,
    )
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError("SearXNG response is not an object")
    return document


def _result_engine(item: dict[str, Any], expected: str) -> str:
    values: list[str] = []
    if isinstance(item.get("engine"), str):
        values.append(item["engine"].strip().lower())
    if isinstance(item.get("engines"), list):
        values.extend(str(value).strip().lower() for value in item["engines"] if isinstance(value, str))
    if expected.lower() not in values:
        raise ValueError("SearXNG result engine changed")
    return expected.lower()


def _validate_result(
    item: object,
    *,
    query: str,
    engine: str,
    category: str,
    position: int,
    endpoint: str,
    language: str,
    time_range: str | None,
    fetched_at: datetime,
) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("SearXNG result item is not an object")
    url = item.get("url")
    title = item.get("title")
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ValueError("SearXNG result URL is invalid")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("SearXNG result URL is unsafe")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("SearXNG result title is invalid")
    content = item.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str) or len(content) > 4_000:
        raise ValueError("SearXNG result content is invalid")
    published_at = item.get("publishedDate")
    if published_at is not None and (not isinstance(published_at, str) or len(published_at) > 80):
        raise ValueError("SearXNG result publication time is invalid")
    result_category = item.get("category", category)
    if not isinstance(result_category, str) or not result_category.strip() or len(result_category) > 50:
        raise ValueError("SearXNG result category is invalid")
    observed_at = fetched_at.isoformat()
    return {
        "source_record_id": _identity(
            "searxng_result", fetched_at.date().isoformat(), query.casefold(), engine.casefold(), url
        ),
        "query": query,
        "engine": _result_engine(item, engine),
        "category": result_category.strip().lower(),
        "position": position,
        "url": url,
        "title": title.strip(),
        "text": content.strip(),
        "published_at": published_at,
        "language": language,
        "time_range": time_range,
        "observed_at": observed_at,
        "scope": "searxng_engine_result_snapshot",
        "source_url": endpoint,
        "collector_version": COLLECTOR_VERSION,
        "sample_complete": True,
        "quality_status": "accepted",
    }


def _failure(exc: Exception) -> dict[str, object]:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        kind = "rate_limit"
    elif status in {401, 403}:
        kind = "access_denied"
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        kind = "dependency_missing"
    elif isinstance(exc, ValueError):
        kind = "schema_drift"
    else:
        kind = "upstream"
    return {"kind": kind, "status": status if isinstance(status, int) else None}


def _engine_reason(value: object, engine: str) -> str | None:
    """Classify upstream diagnostics without persisting arbitrary error text."""
    if not value:
        return None
    if not isinstance(value, list):
        return "upstream"
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2 or item[0] != engine:
            continue
        reason = str(item[1])[:300].casefold()
        if "captcha" in reason:
            return "captcha"
        if "429" in reason or "too many" in reason or "rate" in reason:
            return "rate_limit"
        if "timeout" in reason or "timed out" in reason:
            return "timeout"
        if "403" in reason or "forbidden" in reason or "access denied" in reason:
            return "access_denied"
        return "upstream"
    return "upstream"


def collect(
    job: dict[str, Any],
    *,
    now: datetime | None = None,
    request_page: RequestPage = _request_page,
) -> dict[str, object]:
    fetched_at = now or _utc_now()
    records: list[dict[str, object]] = []
    warnings: set[str] = set()
    failures: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    category = str(job["categories"][0])
    for query in job["queries"]:
        for engine in job["engines"]:
            slice_records: list[dict[str, object]] = []
            seen_urls: set[str] = set()
            complete = True
            reason = None
            for page in range(1, int(job["max_pages"]) + 1):
                params: dict[str, object] = {
                    "q": query,
                    "format": "json",
                    "engines": engine,
                    "language": job["language"],
                    "pageno": page,
                    "safesearch": job["safe_search"],
                }
                if job["time_range"] is not None:
                    params["time_range"] = job["time_range"]
                try:
                    document = request_page(
                        str(job["endpoint"]), params, int(job["request_timeout_seconds"])
                    )
                    items = document.get("results")
                    if not isinstance(items, list):
                        raise ValueError("SearXNG results field changed")
                    if document.get("unresponsive_engines"):
                        warnings.add("unresponsive_engine")
                        complete = False
                        reason = _engine_reason(document.get("unresponsive_engines"), str(engine))
                    if not items:
                        if page == 1:
                            warnings.add("slice_empty")
                        break
                    for item in items:
                        raw_url = item.get("url") if isinstance(item, dict) else None
                        if isinstance(raw_url, str) and raw_url in seen_urls:
                            continue
                        position = len(slice_records) + 1
                        record = _validate_result(
                            item,
                            query=str(query),
                            engine=str(engine),
                            category=category,
                            position=position,
                            endpoint=str(job["endpoint"]),
                            language=str(job["language"]),
                            time_range=job["time_range"],
                            fetched_at=fetched_at,
                        )
                        seen_urls.add(str(record["url"]))
                        slice_records.append(record)
                        if len(slice_records) >= int(job["max_results_per_slice"]):
                            break
                    if len(slice_records) >= int(job["max_results_per_slice"]):
                        break
                except Exception as exc:  # third-party errors become a stable envelope
                    failures.append(_failure(exc))
                    reason = _failure(exc)["kind"]
                    if "timeout" in type(exc).__name__.casefold():
                        reason = "timeout"
                    warnings.add("slice_unavailable")
                    complete = False
                    break
            if not complete:
                for record in slice_records:
                    record["sample_complete"] = False
                    record["quality_status"] = "incomplete"
            records.extend(slice_records)
            diagnostics.append({"query": str(query), "engine": str(engine), "record_count": len(slice_records),
                                "status": ("partial" if slice_records else "failed") if not complete else
                                          ("complete" if slice_records else "empty"),
                                "reason": reason})
            if len(records) > int(job["limit"]):
                return _error(fetched_at, warnings, {"kind": "resource_exhausted", "status": None})

    if not records and failures:
        priority = {"rate_limit": 0, "access_denied": 1, "dependency_missing": 2, "schema_drift": 3, "upstream": 4}
        error = min(failures, key=lambda item: priority.get(str(item.get("kind")), 99))
        return _error(fetched_at, warnings, error, diagnostics)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
        "status": "ok",
        "records": records,
        "warnings": sorted(warnings),
        "fetched_at": fetched_at.isoformat(),
        "error": None,
        "diagnostics": diagnostics,
    }


def _error(fetched_at: datetime, warnings: set[str], error: dict[str, object], diagnostics=None) -> dict[str, object]:
    result = {
        "protocol_version": PROTOCOL_VERSION,
        "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
        "status": "error",
        "records": [],
        "warnings": sorted(warnings),
        "fetched_at": fetched_at.isoformat(),
        "error": error,
    }
    if diagnostics is not None:
        result["diagnostics"] = diagnostics
    return result


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
        result = _error(_utc_now(), set(), {"kind": "invalid_job", "status": None})
    _write_result(args.result, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
