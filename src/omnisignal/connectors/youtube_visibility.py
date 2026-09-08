"""A snapshot connector reusing the existing isolation, archive and durable storage."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from pydantic import ValidationError

from omnisignal.contracts import (Checkpoint, CollectBatch, CollectRequest, ConnectorFailure,
    ConnectorSpec, DeleteBatch, ErrorCategory, HealthReport, HealthStatus, RecordEnvelope)
from omnisignal.governance import SourceApproval
from omnisignal.measurement import ObservationContext
from omnisignal.runtime import IsolatedWorkerError, WorkerErrorKind, run_json_worker
from .archive import FileRawResponseArchive
from .youtube_visibility_analysis import SearchSample, analyze_sample
from .youtube_visibility_policy import YouTubeVisibilityPolicy

TARGET = "https://www.youtube.com/results"


class YouTubeVisibilityConnector:
    def __init__(self, spec: ConnectorSpec, policy: YouTubeVisibilityPolicy, approval: SourceApproval,
                 *, workspace: Path, archive: FileRawResponseArchive | None = None,
                 worker_runner=run_json_worker, observation_context: ObservationContext | None = None):
        self.spec, self.policy, self.approval = spec, policy, approval
        self.workspace, self.archive, self.worker_runner = workspace, archive, worker_runner
        self.closed = False
        self.last_report: dict | None = None
        self.observation_context = observation_context

    async def validate(self) -> None:
        if self.closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "connector is closed")
        valid = (self.spec.id == self.approval.source_id == "youtube_visibility"
                 and self.spec.kind.value == "api" and self.spec.auth.kind.value == "none"
                 and set(self.spec.allowed_targets) == {TARGET}
                 and set(self.spec.field_allowlist) == {"visibility_snapshot"}
                 and self.spec.extractor.kind.value == "plugin"
                 and self.spec.extractor.entrypoint == "omnisignal.connectors.youtube_visibility:YouTubeVisibilityConnector"
                 and self.spec.isolation.mode.value == "process" and self.spec.isolation.kill_switch
                 and self.spec.pagination.kind.value == "none" and self.spec.pagination.max_pages == 1
                 and self.spec.limits.max_attempts == self.spec.limits.max_concurrency == 1
                 and self.spec.limits.requests_per_minute <= self.approval.rate_budget_rpm)
        if not valid:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "video search connector configuration changed")

    async def collect(self, request: CollectRequest) -> CollectBatch:
        await self.validate()
        if request.checkpoint and request.checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint source mismatch")
        try:
            result = await self.worker_runner(module_name="omnisignal.connectors.youtube_visibility_worker",
                document=self.policy.model_dump(mode="json"), workspace=self.workspace,
                timeout_seconds=self.spec.limits.timeout_seconds, max_output_bytes=1_000_000)
        except IsolatedWorkerError as exc:
            category = ErrorCategory.SCHEMA_DRIFT if exc.kind == WorkerErrorKind.INVALID_OUTPUT else ErrorCategory.RUNNER_CRASH
            raise ConnectorFailure(category, "video search worker stopped") from exc
        if "error" in result:
            category = {"dependency_missing": ErrorCategory.INVALID_CONFIG,
                        "schema_drift": ErrorCategory.SCHEMA_DRIFT}.get(result["error"], ErrorCategory.TRANSIENT_UPSTREAM)
            raise ConnectorFailure(category, "video search source unavailable")
        try:
            sample = SearchSample.model_validate(result)
            report = analyze_sample(self.policy, sample)
        except (ValueError, ValidationError) as exc:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "video search sample schema changed") from exc
        if self.observation_context is not None:
            report["observation_context"] = self.observation_context.model_dump(mode="json")
        payload = {"visibility_snapshot": report}
        raw_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identity = hashlib.sha256((self.policy.fingerprint() + sample.fetched_at.isoformat()).encode()).hexdigest()
        receipt = self.archive.store(source_id=self.spec.id, request_url=TARGET, status_code=200,
            headers={}, body={"records": [payload], "record_links": [{"source_record_id": identity, "raw_hash": raw_hash}]},
            collected_at=sample.fetched_at) if self.archive else None
        record = RecordEnvelope(source_id=self.spec.id, source_record_id=identity, collected_at=sample.fetched_at,
            payload=payload, raw_hash=raw_hash, schema_version="1.0", permission=self.approval.permission,
            raw_archive_sha256=receipt.sha256 if receipt else None)
        self.last_report = report
        checkpoint = Checkpoint(source_id=self.spec.id, value={"cycle_complete": True,
            "last_success_at": sample.fetched_at.isoformat(), "quality_status": report["quality_status"],
            "rule_hash": self.policy.fingerprint()}, version=1, updated_at=sample.fetched_at)
        return CollectBatch(records=(record,), next_checkpoint=checkpoint, has_more=False)

    async def health(self) -> HealthReport:
        status = HealthStatus.UNHEALTHY if self.closed else (
            HealthStatus.DEGRADED if self.last_report and self.last_report["quality_status"] != "complete" else HealthStatus.HEALTHY)
        return HealthReport(source_id=self.spec.id, status=status, checked_at=datetime.now(timezone.utc),
                            detail_code="closed" if self.closed else "configured" if not self.last_report else self.last_report["quality_status"])

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        return DeleteBatch(source_id=self.spec.id)

    async def close(self) -> None:
        self.closed = True
