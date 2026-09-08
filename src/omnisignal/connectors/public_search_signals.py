"""Process-isolated connector for anonymous public search-demand signals."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

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
from .public_search_policy import PublicSearchPolicy
from .public_search_protocol import OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION


TRENDS_TARGET = "https://trends.google.com/trends/"
SUGGEST_TARGET = "https://suggestqueries.google.com/complete/search"
WORKER_MODULE = "omnisignal.connectors.public_search_worker"
_METRIC_TYPES = {"relative_interest", "suggestion_rank"}
_METRIC_CONTRACT = {
    "relative_interest": {
        "unit": "index_0_100",
        "scope": "google_trends_same_request_scale",
        "source_url": TRENDS_TARGET,
    },
    "suggestion_rank": {
        "unit": "rank_1_best",
        "scope": "public_autocomplete_snapshot",
        "source_url": SUGGEST_TARGET,
    },
}
_RECORD_FIELDS = {
    "source_record_id",
    "query",
    "related_query",
    "platform",
    "metric_type",
    "value",
    "unit",
    "geo",
    "time_window",
    "observed_at",
    "is_partial",
    "scope",
    "comparison_group",
    "source_url",
    "collector_version",
    "quality_status",
}
WorkerRunner = Callable[..., Awaitable[dict[str, object]]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PublicSearchSignalsConnector:
    def __init__(
        self,
        spec: ConnectorSpec,
        policy: PublicSearchPolicy,
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

    async def validate(self) -> None:
        if self._closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search connector is closed")
        if self.spec.kind != ConnectorKind.API or self.spec.auth.kind != AuthKind.NONE:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search connector requires anonymous API mode")
        if set(self.spec.allowed_targets) != {TRENDS_TARGET, SUGGEST_TARGET}:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "public search targets do not match the fixed allowlist")
        if self.spec.extractor.kind != ExtractorKind.PLUGIN or self.spec.extractor.entrypoint != (
            "omnisignal.connectors.public_search_signals:PublicSearchSignalsConnector"
        ):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search connector entrypoint changed")
        if self.spec.pagination.kind != PaginationKind.NONE or self.spec.pagination.max_pages != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search connector is a one-batch snapshot")
        if self.spec.isolation.mode != IsolationMode.PROCESS or not self.spec.isolation.kill_switch:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "public search worker requires process isolation")
        if self.spec.limits.max_concurrency != 1 or self.spec.limits.max_attempts != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search collection must be serial without retries")
        if self.spec.limits.requests_per_minute > self.approval.rate_budget_rpm:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "public search rate exceeds registry approval")
        if self.approval.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source approval belongs to another connector")
        if set(self.spec.field_allowlist) != _RECORD_FIELDS:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search field contract changed")
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
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search connector is closed")
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
            "keywords": list(self.policy.keywords),
            "geo": self.policy.geo,
            "timeframe": self.policy.timeframe,
            "search_property": self.policy.search_property.value,
            "language": self.policy.language,
            "timezone_minutes": self.policy.timezone_minutes,
            "include_suggestions": self.policy.include_suggestions,
            "max_suggestions": self.policy.max_suggestions,
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
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search worker configuration is invalid") from exc
        except IsolatedWorkerError as exc:
            if exc.kind == WorkerErrorKind.OUTPUT_TOO_LARGE:
                category = ErrorCategory.RESOURCE_EXHAUSTED
            elif exc.kind == WorkerErrorKind.INVALID_OUTPUT:
                category = ErrorCategory.SCHEMA_DRIFT
            else:
                category = ErrorCategory.RUNNER_CRASH
            raise ConnectorFailure(category, "public search worker stopped") from exc

    def _build_batch(self, result: dict[str, object], limit: int) -> CollectBatch:
        allowed = {"protocol_version", "schema_fingerprint", "status", "records", "warnings", "fetched_at", "error"}
        if set(result) != allowed:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search worker envelope changed")
        if result.get("protocol_version") != PROTOCOL_VERSION or result.get("schema_fingerprint") != OUTPUT_SCHEMA_FINGERPRINT:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search worker protocol changed")
        if result.get("status") == "error":
            self._raise_worker_error(result.get("error"))
        if result.get("status") != "ok":
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search worker status is invalid")
        records = result.get("records")
        warnings = result.get("warnings")
        if not isinstance(records, list) or not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search worker payload is invalid")
        if len(records) > limit:
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "public search worker returned too many records")
        collected_at = self._parse_time(result.get("fetched_at"))
        archive_sha256: str | None = None
        projected = [self._enrich_record(self._validate_record(item)) for item in records]
        if self.archive is not None:
            receipt = self.archive.store(
                source_id=self.spec.id,
                request_url=TRENDS_TARGET,
                status_code=200,
                headers={"protocol-version": PROTOCOL_VERSION},
                body={
                    "records": projected,
                    "warnings": sorted(set(warnings)),
                    "record_links": [
                        {"source_record_id": item["source_record_id"], "raw_hash": self._raw_hash(item)}
                        for item in projected
                    ],
                },
                collected_at=collected_at,
            )
            archive_sha256 = receipt.sha256
        envelopes = tuple(self._envelope(item, collected_at, archive_sha256) for item in projected)
        self._last_status = HealthStatus.DEGRADED if warnings else HealthStatus.HEALTHY
        self.last_warnings = tuple(sorted(set(warnings)))
        self._last_detail = sorted(set(warnings))[0] if warnings else "ok"
        checkpoint = Checkpoint(
            source_id=self.spec.id,
            value={
                "policy_hash": self._policy_hash(),
                "cycle_complete": True,
                "last_success_at": collected_at.isoformat(),
                "warnings": sorted(set(warnings)),
            },
            version=1,
            updated_at=collected_at,
        )
        return CollectBatch(records=envelopes, next_checkpoint=checkpoint, has_more=False)

    def _validate_record(self, item: object) -> dict[str, Any]:
        if not isinstance(item, dict) or set(item) != _RECORD_FIELDS:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search record fields changed")
        identity = item.get("source_record_id")
        metric_type = item.get("metric_type")
        value = item.get("value")
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search record identity is invalid")
        if metric_type not in _METRIC_TYPES:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search metric type is invalid")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search metric value is invalid")
        if metric_type == "relative_interest" and not 0 <= value <= 100:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "relative interest left the 0-100 range")
        if metric_type == "suggestion_rank" and not 1 <= value <= self.policy.max_suggestions:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "suggestion rank left the configured range")
        expected = _METRIC_CONTRACT[str(metric_type)]
        for field, expected_value in expected.items():
            if item.get(field) != expected_value:
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, f"public search {field} changed")
        if item.get("platform") != self.policy.search_property.value:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search platform changed")
        if item.get("geo") != (self.policy.geo if metric_type == "relative_interest" else ""):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search geo changed")
        if not isinstance(item.get("query"), str) or item["query"] not in self.policy.keywords:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search query changed")
        if not isinstance(item.get("is_partial"), bool):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search partial marker is invalid")
        if item.get("quality_status") not in {"accepted", "partial_period"}:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search quality status changed")
        if not isinstance(item.get("observed_at"), str) or not item["observed_at"]:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search observation time is invalid")
        if not isinstance(item.get("collector_version"), str) or not item["collector_version"]:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search collector version is invalid")
        if metric_type == "relative_interest":
            if item.get("related_query") is not None or not isinstance(item.get("comparison_group"), str):
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "relative interest comparison metadata changed")
            if item.get("time_window") != self.policy.timeframe:
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "relative interest time window changed")
        else:
            if not isinstance(item.get("related_query"), str) or not item["related_query"]:
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "suggestion text is invalid")
            if item.get("comparison_group") is not None or item.get("time_window") != "daily_snapshot":
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "suggestion snapshot metadata changed")
        return {str(key): value for key, value in item.items()}

    def _enrich_record(self, item: dict[str, Any]) -> dict[str, Any]:
        if self.observation_context is None:
            return item
        context = self.observation_context.model_dump(mode="json")
        identity = hashlib.sha256(
            f"{item['source_record_id']}\0{context['definition_hash']}".encode("utf-8")
        ).hexdigest()
        return {**item, "source_record_id": identity, "observation_context": context}

    def _envelope(
        self,
        item: dict[str, Any],
        collected_at: datetime,
        archive_sha256: str | None,
    ) -> RecordEnvelope:
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
        if checkpoint.source_id != self.spec.id or checkpoint.value.get("policy_hash") != self._policy_hash():
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search checkpoint does not match policy")

    @staticmethod
    def _parse_time(value: object) -> datetime:
        if not isinstance(value, str):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search fetch time is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search fetch time is invalid") from exc
        if parsed.tzinfo is None:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search fetch time requires timezone")
        return parsed

    @staticmethod
    def _raise_worker_error(error: object) -> None:
        if not isinstance(error, dict):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search worker error changed")
        kind = error.get("kind")
        if kind == "rate_limit":
            raise ConnectorFailure(ErrorCategory.RATE_LIMIT, "public search source was rate limited", retry_after_seconds=3600)
        if kind == "dependency_missing" or kind == "invalid_job":
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "public search worker dependency or job is invalid")
        if kind == "schema_drift":
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search upstream schema changed")
        if kind == "resource_exhausted":
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "public search result exceeded the configured limit")
        if kind == "upstream":
            raise ConnectorFailure(ErrorCategory.TRANSIENT_UPSTREAM, "public search upstream is unavailable")
        raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "public search worker error kind changed")
