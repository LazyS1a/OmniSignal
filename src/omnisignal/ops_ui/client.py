"""Small, bounded HTTP client for the read-only operations API."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Mapping
from urllib.parse import urlsplit

import httpx


DEFAULT_API_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TRANSIENT_STATUSES = {502, 503, 504}
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}")


class OpsApiError(RuntimeError):
    """Safe error suitable for display in the operator console."""

    def __init__(self, message: str, *, code: str = "invalid_response", http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class OpsCsvExport:
    content: bytes
    rows: int
    filename: str = "omnisignal-records.csv"


@dataclass(frozen=True, slots=True)
class OpsApiClient:
    base_url: str = DEFAULT_API_BASE_URL
    timeout_seconds: float = 5.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    retries: int = 1
    retry_delay_seconds: float = 0.1

    def __post_init__(self) -> None:
        normalized = _normalize_base_url(self.base_url)
        object.__setattr__(self, "base_url", normalized)
        if not 0.5 <= self.timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0.5 and 30")
        if not 1 <= self.max_response_bytes <= 10 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 byte and 10 MiB")
        if not 0 <= self.retries <= 2:
            raise ValueError("retries must be between 0 and 2")
        if not 0 <= self.retry_delay_seconds <= 1:
            raise ValueError("retry_delay_seconds must be between 0 and 1")

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return self._request_json("GET", path, params=params, attempts=self.retries + 1)

    def whoami(self, bearer_token: str) -> dict[str, object]:
        return self._request_json(
            "GET",
            "/ops/control/whoami",
            bearer_token=bearer_token,
            attempts=1,
        )

    def get_health_json(self, path: str) -> dict[str, object]:
        if path not in {"/health/live", "/health/ready"}:
            raise ValueError("only named health endpoints are allowed")
        return self._request_json("GET", path, attempts=self.retries + 1, health=True)

    def set_source_control(
        self,
        *,
        source_id: str,
        enabled: bool,
        expected_version: int,
        confirmation: str,
        reason: str,
        idempotency_key: str,
        bearer_token: str,
    ) -> dict[str, object]:
        if _SOURCE_ID.fullmatch(source_id) is None:
            raise ValueError("source_id is invalid")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("idempotency_key is invalid")
        return self._request_json(
            "POST",
            f"/ops/sources/{source_id}/control",
            json_body={
                "enabled": enabled,
                "expected_version": expected_version,
                "confirmation": confirmation,
                "reason": reason,
            },
            bearer_token=bearer_token,
            extra_headers={"Idempotency-Key": idempotency_key},
            attempts=1,
        )

    def save_search_profile(self, profile: dict[str, object], *, bearer_token: str) -> dict[str, object]:
        return self._request_json("POST", "/ops/search-profiles", json_body=profile,
                                  bearer_token=bearer_token, attempts=1)

    def create_visual_project(self, project: dict[str, object], *, bearer_token: str) -> dict[str, object]:
        return self._request_json(
            "POST", "/ops/visual/projects", json_body=project,
            bearer_token=bearer_token, attempts=1,
        )

    def create_visual_demo(self, *, bearer_token: str) -> dict[str, object]:
        return self._request_json(
            "POST", "/ops/visual/demo-project", bearer_token=bearer_token, attempts=1,
        )

    def upload_visual_image(self, project_id: str, content: bytes, *, bearer_token: str) -> dict[str, object]:
        _validate_visual_project_id(project_id)
        if not 0 < len(content) <= 5_000_000:
            raise ValueError("PNG must be between 1 byte and 5 MB")
        return self._request_json(
            "POST", f"/ops/visual/projects/{project_id}/image", binary_body=content,
            bearer_token=bearer_token, extra_headers={"Content-Type": "image/png"}, attempts=1,
        )

    def download_visual_image(self, project_id: str, *, bearer_token: str) -> bytes:
        _validate_visual_project_id(project_id)
        response = self._request(
            "GET", f"/ops/visual/projects/{project_id}/image", bearer_token=bearer_token,
            attempts=self.retries + 1, accept="image/png",
        )
        if not response.headers.get("content-type", "").lower().startswith("image/png"):
            raise OpsApiError("视觉工程没有返回 PNG 图片")
        return response.content

    def download_visual_bundle(self, project_id: str, *, bearer_token: str) -> bytes:
        _validate_visual_project_id(project_id)
        response = self._request(
            "GET", f"/ops/visual/projects/{project_id}/bundle", bearer_token=bearer_token,
            attempts=self.retries + 1, accept="application/zip",
        )
        if not response.headers.get("content-type", "").lower().startswith("application/zip"):
            raise OpsApiError("视觉工程没有返回分层 ZIP")
        return response.content

    def start_collection_job(
        self,
        *,
        task_id: str,
        idempotency_key: str,
        bearer_token: str,
    ) -> dict[str, object]:
        if _SOURCE_ID.fullmatch(task_id) is None:
            raise ValueError("task_id is invalid")
        if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            raise ValueError("idempotency_key is invalid")
        return self._request_json(
            "POST",
            "/ops/collection-jobs",
            json_body={"task_id": task_id, "confirmation": f"RUN {task_id}"},
            bearer_token=bearer_token,
            extra_headers={"Idempotency-Key": idempotency_key},
            attempts=1,
        )

    def download_records_csv(
        self,
        *,
        source_id: str = "",
        quality_status: str = "",
        q: str = "",
        limit: int = 1000,
    ) -> OpsCsvExport:
        if source_id and _SOURCE_ID.fullmatch(source_id) is None:
            raise ValueError("source_id is invalid")
        if quality_status and not 1 <= len(quality_status) <= 20:
            raise ValueError("quality_status is invalid")
        if q and not 1 <= len(q) <= 200:
            raise ValueError("q is invalid")
        response = self._request(
            "GET",
            "/ops/exports/records.csv",
            params={
                "source_id": source_id,
                "quality_status": quality_status,
                "q": q,
                "limit": limit,
            },
            attempts=self.retries + 1,
            accept="text/csv",
            limit_max=1000,
        )
        if not response.headers.get("content-type", "").lower().startswith("text/csv"):
            raise OpsApiError("运维 API 没有返回 CSV")
        row_header = response.headers.get("x-omnisignal-export-rows", "")
        if not row_header.isdecimal() or int(row_header) > limit:
            raise OpsApiError("运维 API 返回了无效的导出行数")
        return OpsCsvExport(content=response.content, rows=int(row_header))

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        binary_body: bytes | None = None,
        bearer_token: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        attempts: int,
        health: bool = False,
    ) -> dict[str, object]:
        response = self._request(
            method,
            path,
            params=params,
            json_body=json_body,
            binary_body=binary_body,
            bearer_token=bearer_token,
            extra_headers=extra_headers,
            attempts=attempts,
            accept="application/json",
            health=health,
        )
        try:
            document = response.json()
        except (ValueError, RecursionError) as exc:
            raise OpsApiError("运维 API 没有返回有效 JSON") from exc
        if not isinstance(document, dict):
            raise OpsApiError("运维 API 必须返回 JSON object")
        return document

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        binary_body: bytes | None = None,
        bearer_token: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        attempts: int,
        accept: str,
        limit_max: int = 200,
        health: bool = False,
    ) -> httpx.Response:
        if health:
            if method != "GET" or path not in {"/health/live", "/health/ready"}:
                raise ValueError("only named read-only health endpoints are allowed")
        else:
            _validate_ops_path(path)
        safe_params = _validate_params(params or {}, limit_max=limit_max)
        headers = {"Accept": accept, "Accept-Encoding": "identity", "User-Agent": "OmniSignal-Ops-UI/0.1"}
        if bearer_token is not None:
            if not 32 <= len(bearer_token) <= 512:
                raise ValueError("control token length is invalid")
            headers["Authorization"] = f"Bearer {bearer_token}"
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(attempts):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                    headers=headers,
                ) as client:
                    with client.stream(method, path, params=safe_params, json=json_body, content=binary_body) as response:
                        if response.status_code < 400:
                            response = self._read_bounded(response)
            except httpx.RequestError as exc:
                if attempt + 1 < attempts:
                    _retry_pause(self.retry_delay_seconds)
                    continue
                raise OpsApiError(f"运维 API 暂时不可用（{type(exc).__name__}）", code="transport_error") from exc

            if response.status_code in _TRANSIENT_STATUSES and attempt + 1 < attempts:
                _retry_pause(self.retry_delay_seconds)
                continue
            if response.status_code >= 300:
                raise OpsApiError(f"运维 API 返回 HTTP {response.status_code}", code="http_error", http_status=response.status_code)

            return response

        raise AssertionError("unreachable")

    def _read_bounded(self, response: httpx.Response) -> httpx.Response:
        # This local API does not need compressed exports. Reject unexpected
        # compression before decoding, including potential decompression bombs.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise OpsApiError("运维 API 返回了不支持的压缩响应")
        declared_length = response.headers.get("content-length")
        if declared_length and declared_length.isdecimal() and int(declared_length) > self.max_response_bytes:
            raise OpsApiError("运维 API response exceeded the configured size limit")
        body = bytearray()
        deadline = time.monotonic() + self.timeout_seconds
        # Do not buffer to a fixed chunk size: a trickling response must reach
        # the time-budget check after each received transport chunk.
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline:
                raise OpsApiError("运维 API 响应读取超时")
            if len(body) + len(chunk) > self.max_response_bytes:
                raise OpsApiError("运维 API response exceeded the configured size limit")
            body.extend(chunk)
        return httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("API base URL must be an http(s) origin without credentials, path, query or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_ops_path(path: str) -> None:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith("/ops/"):
        raise ValueError("client only accepts read-only /ops paths")


def _validate_visual_project_id(project_id: str) -> None:
    if re.fullmatch(r"visual_[a-f0-9]{32}", project_id) is None:
        raise ValueError("visual project ID is invalid")


def _validate_params(params: Mapping[str, object], *, limit_max: int = 200) -> dict[str, object]:
    safe = {str(key): value for key, value in params.items() if value is not None and value != ""}
    _bounded_integer(safe, "limit", 1, limit_max)
    _bounded_integer(safe, "offset", 0, 100_000)
    _bounded_integer(safe, "window_hours", 1, 720)
    _bounded_integer(safe, "days", 1, 365)
    return safe


def _bounded_integer(params: Mapping[str, object], key: str, minimum: int, maximum: int) -> None:
    if key not in params:
        return
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")


def _retry_pause(seconds: float) -> None:
    if seconds:
        time.sleep(seconds)
