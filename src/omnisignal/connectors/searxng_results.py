"""Process-isolated connector for self-hosted SearXNG result snapshots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    ExtractorKind,
    HealthReport,
    HealthStatus,
    IsolationMode,
    PaginationKind,
    RecordEnvelope,
)
from omnisignal.governance import SourceApproval
from omnisignal.measurement import ObservationContext
from omnisignal.runtime import IsolatedWorkerError, WorkerErrorKind, run_json_worker

from .archive import FileRawResponseArchive
from .searxng_policy import SearXNGPolicy
from .searxng_protocol import OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION


WORKER_MODULE = "omnisignal.connectors.searxng_worker"
SCOPE = "searxng_engine_result_snapshot"
_WARNINGS = {"slice_empty", "slice_unavailable", "unresponsive_engine"}
_RECORD_FIELDS = {
    "source_record_id",
    "query",
    "engine",
    "category",
    "position",
    "url",
    "title",
    "text",
    "published_at",
    "language",
    "time_range",
    "observed_at",
    "scope",
    "source_url",
    "collector_version",
    "sample_complete",
    "quality_status",
}
WorkerRunner = Callable[..., Awaitable[dict[str, object]]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SearXNGResultsConnector:
    def __init__(
        self,
        spec: ConnectorSpec,
        policy: SearXNGPolicy,
        approval: SourceApproval,
        *,
        workspace: Path,
        archive: FileRawResponseArchive | None = None,
        worker_runner: WorkerRunner = run_json_worker,
        now: Callable[[], datetime] = _utc_now,
        observation_context: ObservationContext | None = None,
    ) -> None:
        self.spec = spec
        self.policy = policy
        self.approval = approval
        self.workspace = workspace.resolve()
        self.archive = archive
        self._worker_runner = worker_runner
        self._now = now
        self.observation_context = observation_context
        self._validated = False
        self._closed = False
        self._last_status = HealthStatus.HEALTHY
        self._last_detail = "configured"
        self.last_warnings: tuple[str, ...] = ()
        self.last_diagnostics: list[dict[str, object]] = []

    async def validate(self) -> None:
        if self._closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG connector is closed")
        if self.spec.kind != ConnectorKind.API or self.spec.auth.kind != AuthKind.NONE:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG connector requires anonymous API mode")
        if self.spec.allowed_targets != (self.policy.endpoint,):
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "SearXNG target differs from the fixed policy")
        if self.spec.extractor.kind != ExtractorKind.PLUGIN or self.spec.extractor.entrypoint != (
            "omnisignal.connectors.searxng_results:SearXNGResultsConnector"
        ):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG connector entrypoint changed")
        if self.spec.pagination.kind != PaginationKind.NONE or self.spec.pagination.max_pages != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG worker owns bounded pagination")
        if self.spec.isolation.mode != IsolationMode.PROCESS or not self.spec.isolation.kill_switch:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "SearXNG worker requires process isolation")
        if self.spec.limits.max_concurrency != 1 or self.spec.limits.max_attempts != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG collection must be serial without retries")
        if self.spec.limits.requests_per_minute > self.approval.rate_budget_rpm:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "SearXNG rate exceeds registry approval")
        if self.approval.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source approval belongs to another connector")
        if set(self.spec.field_allowlist) != _RECORD_FIELDS:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG field contract changed")
        if self.policy.maximum_records > self.spec.limits.max_records_per_run:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG policy can exceed the connector record limit")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._validated = True

    async def health(self) -> HealthReport:
        if not self._validated and not self._closed:
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
            status=HealthStatus.UNHEALTHY if self._closed else self._last_status,
            checked_at=self._now(),
            detail_code="closed" if self._closed else self._last_detail,
        )

    async def collect(self, request: CollectRequest) -> CollectBatch:
        if self._closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG connector is closed")
        if not self._validated:
            await self.validate()
        self._validate_checkpoint(request.checkpoint)
        limit = min(request.limit, self.spec.limits.max_records_per_run)
        result = await self._run_worker(limit)
        return self._build_batch(result, limit)

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        del checkpoint
        return DeleteBatch(source_id=self.spec.id)

    async def close(self) -> None:
        self._closed = True

    async def _run_worker(self, limit: int) -> dict[str, object]:
        document: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "endpoint": self.policy.endpoint,
            "queries": list(self.policy.queries),
            "engines": list(self.policy.engines),
            "categories": list(self.policy.categories),
            "language": self.policy.language,
            "time_range": self.policy.time_range,
            "safe_search": self.policy.safe_search,
            "max_pages": self.policy.max_pages,
            "max_results_per_slice": self.policy.max_results_per_slice,
            "request_timeout_seconds": self.policy.request_timeout_seconds,
            "limit": limit,
        }
        try:
            return await self._worker_runner(
                module_name=WORKER_MODULE,
                document=document,
                workspace=self.workspace,
                timeout_seconds=self.spec.limits.timeout_seconds,
                max_output_bytes=self.policy.max_output_bytes,
            )
        except ValueError as exc:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG worker configuration is invalid") from exc
        except IsolatedWorkerError as exc:
            if exc.kind == WorkerErrorKind.OUTPUT_TOO_LARGE:
                category = ErrorCategory.RESOURCE_EXHAUSTED
            elif exc.kind == WorkerErrorKind.INVALID_OUTPUT:
                category = ErrorCategory.SCHEMA_DRIFT
            else:
                category = ErrorCategory.RUNNER_CRASH
            raise ConnectorFailure(category, "SearXNG worker stopped") from exc

    def _build_batch(self, result: dict[str, object], limit: int) -> CollectBatch:
        allowed = {"protocol_version", "schema_fingerprint", "status", "records", "warnings", "fetched_at", "error"}
        if set(result) not in (allowed, allowed | {"diagnostics"}):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker envelope changed")
        if result.get("protocol_version") != PROTOCOL_VERSION or result.get("schema_fingerprint") != OUTPUT_SCHEMA_FINGERPRINT:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker protocol changed")
        if result.get("status") not in {"ok", "error"}:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker status is invalid")
        records = result.get("records")
        warnings = result.get("warnings")
        if (
            not isinstance(records, list)
            or not isinstance(warnings, list)
            or any(not isinstance(item, str) or item not in _WARNINGS for item in warnings)
        ):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker payload is invalid")
        if len(records) > limit:
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "SearXNG worker returned too many records")
        collected_at = self._parse_time(result.get("fetched_at"), "fetch")
        projected = [self._enrich_record(self._validate_record(item)) for item in records]
        diagnostics = result.get("diagnostics", [])
        if not isinstance(diagnostics, list) or len(diagnostics) > 48:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "invalid search diagnostics")
        pairs = set()
        for item in diagnostics:
            if not isinstance(item, dict) or set(item) != {"query", "engine", "record_count", "status", "reason"}:
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "invalid search diagnostic fields")
            if (not all(isinstance(item[k], str) for k in ("query", "engine", "status"))
                or item["reason"] is not None and not isinstance(item["reason"], str)):
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "invalid search diagnostic types")
            pair = (item["query"], item["engine"])
            if (pair in pairs or item["query"] not in self.policy.queries or item["engine"] not in self.policy.engines
                or type(item["record_count"]) is not int
                or item["record_count"] != sum(r["query"] == pair[0] and r["engine"] == pair[1] for r in projected)
                or item["status"] not in {"complete", "empty", "partial", "failed"}
                or item["reason"] not in {None, "captcha", "rate_limit", "timeout", "access_denied", "upstream", "schema_drift", "dependency_missing"}):
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "invalid search diagnostic values")
            pairs.add(pair)
            count = item["record_count"]
            state = item["status"]
            if ((state in {"complete", "partial"}) != (count > 0)
                or (state in {"failed", "partial"}) != (item["reason"] is not None)):
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "inconsistent search diagnostic state")
        if "diagnostics" in result and pairs != {(q, e) for q in self.policy.queries for e in self.policy.engines}:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "incomplete search diagnostics")
        self.last_diagnostics = diagnostics
        self.last_warnings = tuple(sorted(set(warnings)))
        if result.get("status") == "error":
            self._raise_worker_error(result.get("error"))
        identities = [str(item["source_record_id"]) for item in projected]
        if len(set(identities)) != len(identities):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker returned duplicate identities")
        archive_sha256: str | None = None
        if self.archive is not None:
            receipt = self.archive.store(
                source_id=self.spec.id,
                request_url=self.policy.endpoint,
                status_code=200,
                headers={"protocol-version": PROTOCOL_VERSION},
                body={
                    "records": projected,
                    "warnings": sorted(set(warnings)),
                    "diagnostics": diagnostics,
                    "record_links": [
                        {"source_record_id": item["source_record_id"], "raw_hash": self._raw_hash(item)}
                        for item in projected
                    ],
                },
                collected_at=collected_at,
            )
            archive_sha256 = receipt.sha256
        envelopes = tuple(self._envelope(item, collected_at, archive_sha256) for item in projected)
        self.last_warnings = tuple(sorted(set(warnings)))
        self._last_status = HealthStatus.DEGRADED if warnings else HealthStatus.HEALTHY
        self._last_detail = self.last_warnings[0] if self.last_warnings else "ok"
        checkpoint = Checkpoint(
            source_id=self.spec.id,
            value={
                "policy_hash": self._policy_hash(),
                "cycle_complete": True,
                "last_success_at": collected_at.isoformat(),
                "warnings": list(self.last_warnings),
            },
            version=1,
            updated_at=collected_at,
        )
        return CollectBatch(records=envelopes, next_checkpoint=checkpoint, has_more=False)

    def _validate_record(self, item: object) -> dict[str, Any]:
        if not isinstance(item, dict) or set(item) != _RECORD_FIELDS:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG record fields changed")
        identity = item.get("source_record_id")
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG record identity is invalid")
        if item.get("query") not in self.policy.queries or item.get("engine") not in self.policy.engines:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG query or engine left policy")
        if item.get("category") not in self.policy.categories:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG category left policy")
        position = item.get("position")
        if not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= self.policy.max_results_per_slice:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG result position is invalid")
        url = item.get("url")
        if not isinstance(url, str) or len(url) > 4096:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG result URL is invalid")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG result URL is unsafe")
        if not isinstance(item.get("title"), str) or not item["title"] or len(item["title"]) > 500:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG result title is invalid")
        if not isinstance(item.get("text"), str) or len(item["text"]) > 4_000:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG result text is invalid")
        if item.get("published_at") is not None and not isinstance(item.get("published_at"), str):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG publication time is invalid")
        if item.get("language") != self.policy.language or item.get("time_range") != self.policy.time_range:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG query context changed")
        self._parse_time(item.get("observed_at"), "observation")
        if item.get("scope") != SCOPE or item.get("source_url") != self.policy.endpoint:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG result scope changed")
        if not isinstance(item.get("collector_version"), str) or not item["collector_version"]:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG collector version is invalid")
        complete = item.get("sample_complete")
        expected_quality = "accepted" if complete is True else "incomplete"
        if not isinstance(complete, bool) or item.get("quality_status") != expected_quality:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG sample quality is inconsistent")
        return {str(key): value for key, value in item.items()}

    def _enrich_record(self, item: dict[str, Any]) -> dict[str, Any]:
        if self.observation_context is None:
            return item
        context = self.observation_context.model_dump(mode="json")
        identity = hashlib.sha256(
            f"{item['source_record_id']}\0{context['definition_hash']}".encode("utf-8")
        ).hexdigest()
        return {**item, "source_record_id": identity, "observation_context": context}

    def _envelope(self, item: dict[str, Any], collected_at: datetime, archive_sha256: str | None) -> RecordEnvelope:
        payload = {key: value for key, value in item.items() if key != "source_record_id"}
        return RecordEnvelope(
            source_id=self.spec.id,
            source_record_id=str(item["source_record_id"]),
            collected_at=collected_at,
            payload=payload,
            raw_hash=self._raw_hash(item),
            schema_version=self.spec.output_schema_version,
            permission=self.approval.permission,
            raw_archive_sha256=archive_sha256,
        )

    @staticmethod
    def _raw_hash(item: dict[str, Any]) -> str:
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _policy_hash(self) -> str:
        encoded = json.dumps(self.policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _validate_checkpoint(self, checkpoint: Checkpoint | None) -> None:
        if checkpoint is None:
            return
        if checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG checkpoint belongs to another source")
        # This worker produces a whole bounded snapshot, not a resumable cursor.
        # A completed prior observation must not pin all future tasks to its policy.
        if checkpoint.value.get("cycle_complete") is True:
            return
        if checkpoint.value.get("policy_hash") != self._policy_hash():
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG checkpoint does not match policy")

    @staticmethod
    def _parse_time(value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, f"SearXNG {label} time is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, f"SearXNG {label} time is invalid") from exc
        if parsed.tzinfo is None:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, f"SearXNG {label} time requires timezone")
        return parsed

    @staticmethod
    def _raise_worker_error(error: object) -> None:
        if not isinstance(error, dict):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker error changed")
        kind = error.get("kind")
        if kind == "rate_limit":
            raise ConnectorFailure(ErrorCategory.RATE_LIMIT, "SearXNG source was rate limited", retry_after_seconds=3600)
        if kind == "access_denied":
            raise ConnectorFailure(ErrorCategory.PERMISSION, "SearXNG JSON endpoint denied access")
        if kind in {"dependency_missing", "invalid_job"}:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "SearXNG worker dependency or job is invalid")
        if kind == "schema_drift":
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG upstream schema changed")
        if kind == "resource_exhausted":
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "SearXNG result exceeded the configured limit")
        if kind == "upstream":
            raise ConnectorFailure(ErrorCategory.TRANSIENT_UPSTREAM, "SearXNG local service is unavailable")
        raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "SearXNG worker error kind changed")
