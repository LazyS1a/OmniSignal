"""GitHub public Issue/PR search connector using the official REST API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from omnisignal.contracts import (
    AuthKind,
    Checkpoint,
    CollectBatch,
    CollectRequest,
    ConnectorFailure,
    ConnectorKind,
    ConnectorSpec,
    DeleteBatch,
    ErrorCategory,
    HealthReport,
    HealthStatus,
    PaginationKind,
    RecordEnvelope,
)
from omnisignal.secrets import MissingSecretError, resolve_secret

from .archive import FileRawResponseArchive


GITHUB_SEARCH_ENDPOINT = "https://api.github.com/search/issues"
GITHUB_API_VERSION = "2022-11-28"
_MAX_INLINE_RETRY_SECONDS = 60
_PAYLOAD_FIELDS = frozenset(
    {
        "api_url",
        "html_url",
        "repository_url",
        "number",
        "title",
        "body",
        "state",
        "created_at",
        "updated_at",
        "closed_at",
        "is_pull_request",
    }
)
_CHECKPOINT_FIELD = "source_record_id"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GitHubIssueSearchConnector:
    """Collect a bounded page of public issues and pull requests."""

    def __init__(
        self,
        spec: ConnectorSpec,
        *,
        query: str,
        archive: FileRawResponseArchive | None = None,
        client: httpx.AsyncClient | None = None,
        secret_resolver: Callable[[str], str] = resolve_secret,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.spec = spec
        self.query = query.strip()
        self.archive = archive
        self._client = client
        self._owns_client = client is None
        self._secret_resolver = secret_resolver
        self._sleep = sleep
        self._now = now
        self._validated = False
        self._last_status = HealthStatus.HEALTHY
        self._last_detail = "configured"

    async def validate(self) -> None:
        if self.spec.kind != ConnectorKind.API:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub connector requires kind=api")
        if self.spec.allowed_targets != (GITHUB_SEARCH_ENDPOINT,):
            raise ConnectorFailure(
                ErrorCategory.POLICY_VIOLATION,
                "GitHub connector may only access the official issue search endpoint",
            )
        if self.spec.pagination.kind != PaginationKind.LINK_HEADER:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub connector requires link_header pagination")
        if self.spec.pagination.page_size > 100:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub page_size cannot exceed 100")
        if self.spec.limits.max_concurrency != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub connector must run serially")
        if not self.query or len(self.query) > 256 or "\n" in self.query or "\r" in self.query:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub query must contain 1-256 safe characters")

        fields = set(self.spec.field_allowlist)
        unsupported = fields - _PAYLOAD_FIELDS - {_CHECKPOINT_FIELD}
        if unsupported:
            raise ConnectorFailure(
                ErrorCategory.INVALID_CONFIG,
                "GitHub field allowlist contains unsupported fields",
                details={"unsupported_fields": sorted(unsupported)},
            )
        if _CHECKPOINT_FIELD not in fields:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "field_allowlist must include source_record_id")
        if "api_url" not in fields:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "field_allowlist must include api_url")
        if self.spec.auth.kind not in {AuthKind.NONE, AuthKind.BEARER}:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub connector supports none or bearer auth")
        if self.spec.auth.kind == AuthKind.BEARER:
            try:
                self._secret_resolver(self.spec.auth.secret_ref or "")
            except MissingSecretError as exc:
                raise ConnectorFailure(ErrorCategory.AUTHENTICATION, str(exc)) from exc
        self._validated = True

    async def health(self) -> HealthReport:
        if not self._validated:
            try:
                await self.validate()
            except ConnectorFailure as exc:
                return HealthReport(
                    source_id=self.spec.id,
                    status=HealthStatus.PAUSED,
                    checked_at=self._now(),
                    detail_code=exc.category.value,
                )
        return HealthReport(
            source_id=self.spec.id,
            status=self._last_status,
            checked_at=self._now(),
            detail_code=self._last_detail,
        )

    async def collect(self, request: CollectRequest) -> CollectBatch:
        if not self._validated:
            await self.validate()

        first_url = self._first_url(request.limit)
        checkpoint_value = self._checkpoint_value(request.checkpoint, first_url)
        request_url = str(checkpoint_value["request_url"])
        page_number = int(checkpoint_value["page"])
        self._validate_request_url(request_url)

        etags = dict(checkpoint_value["etags"])
        next_by_url = dict(checkpoint_value["next_by_url"])
        ids_by_url = dict(checkpoint_value["ids_by_url"])
        total_by_url = dict(checkpoint_value["total_by_url"])
        incomplete_by_url = dict(checkpoint_value["incomplete_by_url"])
        cycle_seen_ids = {str(value) for value in checkpoint_value["cycle_seen_ids"]}
        previous_complete_ids = {str(value) for value in checkpoint_value["previous_complete_ids"]}
        missing_counts = {str(key): int(value) for key, value in checkpoint_value["missing_counts"].items()}
        pending_deletions = {str(value) for value in checkpoint_value["pending_deletions"]}
        has_previous_snapshot = bool(checkpoint_value["has_previous_snapshot"])
        headers = self._request_headers(etags.get(request_url))
        response = await self._request_with_retry(request_url, headers)
        collected_at = self._now()

        if response.status_code == 304:
            following_url = next_by_url.get(request_url)
            records: tuple[RecordEnvelope, ...] = ()
            if request_url not in ids_by_url or request_url not in total_by_url:
                raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint cannot replay a 304 response")
            page_ids = {str(value) for value in ids_by_url[request_url]}
            total_count = int(total_by_url[request_url])
            incomplete = bool(incomplete_by_url.get(request_url, False))
        else:
            document = self._response_document(response)
            projected_items = tuple(self._project_item(item) for item in document["items"])
            archive_sha256: str | None = None
            following_url = self._next_url(response)
            page_ids = {str(item["id"]) for item in document["items"]}
            total_count = int(document["total_count"])
            incomplete = bool(document["incomplete_results"])

            etag = response.headers.get("etag")
            if etag:
                etags[request_url] = etag
            next_by_url[request_url] = following_url
            ids_by_url[request_url] = sorted(page_ids)
            total_by_url[request_url] = total_count
            incomplete_by_url[request_url] = incomplete
            for cache in (etags, next_by_url, ids_by_url, total_by_url, incomplete_by_url):
                self._bound_cache(cache)
            if self.archive is not None:
                receipt = self.archive.store(
                    source_id=self.spec.id,
                    request_url=request_url,
                    status_code=response.status_code,
                    headers=response.headers,
                    body={
                        "total_count": document["total_count"],
                        "incomplete_results": incomplete,
                        "items": [
                            {"source_record_id": str(item["id"]), **payload}
                            for item, payload in zip(document["items"], projected_items, strict=True)
                        ],
                        "record_links": [
                            {
                                "source_record_id": str(item["id"]),
                                "raw_hash": self._payload_raw_hash(payload),
                            }
                            for item, payload in zip(document["items"], projected_items, strict=True)
                        ],
                    },
                    collected_at=collected_at,
                )
                archive_sha256 = receipt.sha256
            records = tuple(
                self._to_envelope(item, payload, collected_at, archive_sha256)
                for item, payload in zip(document["items"], projected_items, strict=True)
            )

        cycle_seen_ids.update(page_ids)
        page_limit_reached = page_number >= self.spec.pagination.max_pages
        has_more = bool(following_url) and not page_limit_reached
        safe_complete_snapshot = (
            not has_more
            and not incomplete
            and not (page_limit_reached and bool(following_url))
            and total_count <= 1000
            and len(cycle_seen_ids) == total_count
        )
        if safe_complete_snapshot:
            pending_deletions.difference_update(cycle_seen_ids)
            if has_previous_snapshot:
                candidates = set(missing_counts) | (previous_complete_ids - cycle_seen_ids)
                next_missing_counts: dict[str, int] = {}
                for source_record_id in candidates:
                    if source_record_id in cycle_seen_ids:
                        continue
                    count = missing_counts.get(source_record_id, 0) + 1
                    if count >= 2:
                        pending_deletions.add(source_record_id)
                    else:
                        next_missing_counts[source_record_id] = count
                missing_counts = next_missing_counts
            previous_complete_ids = set(cycle_seen_ids)
            has_previous_snapshot = True
        cycle_safe = safe_complete_snapshot
        if not has_more:
            cycle_seen_ids = set()

        next_request_url = str(following_url) if has_more else first_url
        next_page = page_number + 1 if has_more else 1
        next_checkpoint = Checkpoint(
            source_id=self.spec.id,
            value={
                "query_hash": self._query_hash(),
                "request_url": next_request_url,
                "page": next_page,
                "etags": etags,
                "next_by_url": next_by_url,
                "ids_by_url": ids_by_url,
                "total_by_url": total_by_url,
                "incomplete_by_url": incomplete_by_url,
                "cycle_seen_ids": sorted(cycle_seen_ids),
                "previous_complete_ids": sorted(previous_complete_ids),
                "missing_counts": missing_counts,
                "pending_deletions": sorted(pending_deletions),
                "has_previous_snapshot": has_previous_snapshot,
                "cycle_complete": not has_more,
                "cycle_safe_snapshot": cycle_safe,
                "cycle_incomplete_results": incomplete,
                "cycle_page_limit_reached": page_limit_reached and bool(following_url),
            },
            version=1,
            updated_at=collected_at,
        )
        self._last_status = HealthStatus.DEGRADED if incomplete else HealthStatus.HEALTHY
        self._last_detail = "incomplete_results" if incomplete else "ok"
        return CollectBatch(records=records, next_checkpoint=next_checkpoint, has_more=has_more)

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        if checkpoint is None:
            return DeleteBatch(source_id=self.spec.id)
        if checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint belongs to another source")
        pending = checkpoint.value.get("pending_deletions", [])
        if not isinstance(pending, list) or any(not isinstance(value, str) for value in pending):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint has invalid pending deletions")
        next_value = dict(checkpoint.value)
        next_value["pending_deletions"] = []
        next_checkpoint = Checkpoint(
            source_id=checkpoint.source_id,
            value=next_value,
            version=checkpoint.version,
            updated_at=self._now(),
        )
        return DeleteBatch(
            source_id=self.spec.id,
            source_record_ids=tuple(sorted(set(pending))),
            next_checkpoint=next_checkpoint,
        )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    def _first_url(self, request_limit: int) -> str:
        page_size = min(request_limit, self.spec.pagination.page_size, 100)
        query = urlencode(
            {
                "q": self.query,
                "sort": "updated",
                "order": "desc",
                "per_page": page_size,
                "page": 1,
            }
        )
        return f"{GITHUB_SEARCH_ENDPOINT}?{query}"

    def _checkpoint_value(self, checkpoint: Checkpoint | None, first_url: str) -> dict[str, Any]:
        if checkpoint is None:
            return {
                "request_url": first_url,
                "page": 1,
                "etags": {},
                "next_by_url": {},
                "ids_by_url": {},
                "total_by_url": {},
                "incomplete_by_url": {},
                "cycle_seen_ids": [],
                "previous_complete_ids": [],
                "missing_counts": {},
                "pending_deletions": [],
                "has_previous_snapshot": False,
            }
        if checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint belongs to another source")
        if checkpoint.value.get("query_hash") != self._query_hash():
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint query does not match connector query")
        value = checkpoint.value
        request_url = value.get("request_url")
        page = value.get("page")
        etags = value.get("etags", {})
        next_by_url = value.get("next_by_url", {})
        ids_by_url = value.get("ids_by_url", {})
        total_by_url = value.get("total_by_url", {})
        incomplete_by_url = value.get("incomplete_by_url", {})
        cycle_seen_ids = value.get("cycle_seen_ids", [])
        previous_complete_ids = value.get("previous_complete_ids", [])
        missing_counts = value.get("missing_counts", {})
        pending_deletions = value.get("pending_deletions", [])
        has_previous_snapshot = value.get("has_previous_snapshot", False)
        if not isinstance(request_url, str) or not isinstance(page, int):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint has an invalid cursor")
        caches = (etags, next_by_url, ids_by_url, total_by_url, incomplete_by_url, missing_counts)
        lists = (cycle_seen_ids, previous_complete_ids, pending_deletions)
        if any(not isinstance(cache, dict) for cache in caches) or any(not isinstance(values, list) for values in lists):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint has an invalid cache")
        return {
            "request_url": request_url,
            "page": page,
            "etags": etags,
            "next_by_url": next_by_url,
            "ids_by_url": ids_by_url,
            "total_by_url": total_by_url,
            "incomplete_by_url": incomplete_by_url,
            "cycle_seen_ids": cycle_seen_ids,
            "previous_complete_ids": previous_complete_ids,
            "missing_counts": missing_counts,
            "pending_deletions": pending_deletions,
            "has_previous_snapshot": has_previous_snapshot,
        }

    def _request_headers(self, etag: object) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "OmniSignal/0.1",
        }
        if isinstance(etag, str) and etag:
            headers["If-None-Match"] = etag
        if self.spec.auth.kind == AuthKind.BEARER:
            try:
                token = self._secret_resolver(self.spec.auth.secret_ref or "")
            except MissingSecretError as exc:
                raise ConnectorFailure(ErrorCategory.AUTHENTICATION, str(exc)) from exc
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _request_with_retry(self, url: str, headers: dict[str, str]) -> httpx.Response:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=self.spec.limits.timeout_seconds,
                follow_redirects=False,
            )
            self._client = client

        attempts = self.spec.limits.max_attempts
        last_failure: ConnectorFailure | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await client.get(url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_failure = ConnectorFailure(
                    ErrorCategory.TRANSIENT_UPSTREAM,
                    "GitHub request failed before a response was received",
                    details={"attempt": attempt, "exception_type": type(exc).__name__},
                )
                if attempt < attempts:
                    await self._sleep(min(2 ** (attempt - 1), _MAX_INLINE_RETRY_SECONDS))
                    continue
                raise last_failure from exc

            if response.status_code in {200, 304}:
                return response
            failure = self._classify_response(response)
            last_failure = failure
            if failure.category not in {ErrorCategory.RATE_LIMIT, ErrorCategory.TRANSIENT_UPSTREAM}:
                raise failure
            delay = failure.retry_after_seconds or min(2 ** (attempt - 1), _MAX_INLINE_RETRY_SECONDS)
            if attempt >= attempts or delay > _MAX_INLINE_RETRY_SECONDS:
                raise failure
            await self._sleep(delay)

        raise last_failure or ConnectorFailure(ErrorCategory.UNKNOWN, "GitHub request failed")

    def _classify_response(self, response: httpx.Response) -> ConnectorFailure:
        status = response.status_code
        message = self._safe_message(response)
        details = {"status_code": status}
        if status == 401:
            return ConnectorFailure(ErrorCategory.AUTHENTICATION, "GitHub authentication failed", details=details)
        if status in {403, 429}:
            is_limited = (
                status == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in message.lower()
                or response.headers.get("retry-after") is not None
            )
            if is_limited:
                wait = self._retry_after(response)
                return ConnectorFailure(
                    ErrorCategory.RATE_LIMIT,
                    "GitHub rate limit reached",
                    retry_after_seconds=wait,
                    details=details,
                )
            return ConnectorFailure(ErrorCategory.PERMISSION, "GitHub access was forbidden", details=details)
        if status == 404:
            return ConnectorFailure(ErrorCategory.PERMISSION, "GitHub endpoint or resource is not accessible", details=details)
        if status == 410:
            return ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub API version or endpoint is retired", details=details)
        if status == 422:
            return ConnectorFailure(ErrorCategory.INVALID_CONFIG, "GitHub rejected the search query", details=details)
        if 500 <= status <= 599:
            return ConnectorFailure(
                ErrorCategory.TRANSIENT_UPSTREAM,
                "GitHub service is temporarily unavailable",
                details=details,
            )
        if 300 <= status <= 399:
            return ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "GitHub redirect was not followed", details=details)
        return ConnectorFailure(ErrorCategory.UNKNOWN, "Unexpected GitHub response", details=details)

    def _retry_after(self, response: httpx.Response) -> int:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return max(1, int(retry_after))
        reset = response.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            seconds = math.ceil(int(reset) - self._now().timestamp())
            return max(1, seconds)
        return 60

    def _response_document(self, response: httpx.Response) -> dict[str, Any]:
        try:
            document = response.json()
        except ValueError as exc:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub response root is not an object")
        if not isinstance(document.get("items"), list):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub response has no items array")
        if not isinstance(document.get("total_count"), int):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub response has no numeric total_count")
        if not isinstance(document.get("incomplete_results"), bool):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub response has no incomplete_results flag")
        for item in document["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub item has no stable numeric id")
        return document

    def _project_item(self, item: dict[str, Any]) -> dict[str, Any]:
        available = {
            "api_url": item.get("url"),
            "html_url": item.get("html_url"),
            "repository_url": item.get("repository_url"),
            "number": item.get("number"),
            "title": item.get("title"),
            "body": item.get("body"),
            "state": item.get("state"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "closed_at": item.get("closed_at"),
            "is_pull_request": "pull_request" in item,
        }
        allowed = set(self.spec.field_allowlist) - {_CHECKPOINT_FIELD}
        return {key: available[key] for key in available if key in allowed}

    def _to_envelope(
        self,
        item: dict[str, Any],
        payload: dict[str, Any],
        collected_at: datetime,
        archive_sha256: str | None = None,
    ) -> RecordEnvelope:
        source_record_id = str(item["id"])
        return RecordEnvelope(
            source_id=self.spec.id,
            source_record_id=source_record_id,
            collected_at=collected_at,
            payload=payload,
            raw_hash=self._payload_raw_hash(payload),
            schema_version=self.spec.output_schema_version,
            permission="github_public_rest_api",
            raw_archive_sha256=archive_sha256,
        )

    @staticmethod
    def _payload_raw_hash(payload: dict[str, Any]) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _next_url(self, response: httpx.Response) -> str | None:
        next_link = response.links.get("next")
        if not next_link:
            return None
        url = next_link.get("url")
        if not isinstance(url, str):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "GitHub next link has no URL")
        self._validate_request_url(url)
        return url

    def _validate_request_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.port is not None:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "GitHub pagination URL left the approved host")
        if parsed.path != "/search/issues" or parsed.username or parsed.password or parsed.fragment:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "GitHub pagination URL left the approved endpoint")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {"q", "sort", "order", "per_page", "page"}:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "GitHub pagination URL added unexpected parameters")
        if query.get("q") != [self.query]:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "GitHub pagination URL changed the search query")

    def _query_hash(self) -> str:
        return hashlib.sha256(self.query.encode("utf-8")).hexdigest()

    def _bound_cache(self, cache: dict[str, Any]) -> None:
        maximum = self.spec.pagination.max_pages
        while len(cache) > maximum:
            cache.pop(next(iter(cache)))

    @staticmethod
    def _safe_message(response: httpx.Response) -> str:
        try:
            document = response.json()
        except ValueError:
            return ""
        if isinstance(document, dict) and isinstance(document.get("message"), str):
            return document["message"][:200]
        return ""
