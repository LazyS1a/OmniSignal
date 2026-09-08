"""Process-isolated compliant static-web connector backed by Scrapy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    RecordEnvelope,
)
from omnisignal.governance import SourceApproval

from .archive import FileRawResponseArchive
from .static_web_policy import StaticWebPolicy


_SUPPORTED_FIELDS = frozenset(
    {
        "source_record_id",
        "url",
        "published_at",
        "title",
        "text",
        "quality_status",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StaticWebConnector:
    def __init__(
        self,
        spec: ConnectorSpec,
        policy: StaticWebPolicy,
        approval: SourceApproval,
        *,
        workspace: Path,
        archive: FileRawResponseArchive | None = None,
    ) -> None:
        self.spec = spec
        self.policy = policy
        self.approval = approval
        self.workspace = workspace.resolve()
        self.archive = archive
        self._validated = False
        self._closed = False
        self._last_status = HealthStatus.HEALTHY
        self._last_detail = "configured"

    async def validate(self) -> None:
        if self._closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "static web connector is closed")
        if self.spec.kind != ConnectorKind.HTML:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "static web connector requires kind=html")
        if self.approval.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source approval belongs to another connector")
        if self.spec.limits.requests_per_minute > self.approval.rate_budget_rpm:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "connector rate exceeds approved budget")
        if self.spec.auth.kind != AuthKind.NONE:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "static web connector does not accept credentials")
        if self.spec.extractor.kind != ExtractorKind.TRAFILATURA or not self.spec.extractor.selector:
            raise ConnectorFailure(
                ErrorCategory.INVALID_CONFIG,
                "static web connector requires a Trafilatura extractor and snapshot CSS selector",
            )
        if self.spec.isolation.mode != IsolationMode.PROCESS or not self.spec.isolation.kill_switch:
            raise ConnectorFailure(
                ErrorCategory.INVALID_CONFIG,
                "static web connector requires process isolation and a kill switch",
            )
        if self.spec.limits.max_concurrency != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "static web connector must use concurrency 1")
        if set(self.spec.allowed_targets) != set(self.policy.allowed_url_prefixes):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "spec targets must exactly match web policy prefixes")
        unsupported = set(self.spec.field_allowlist) - _SUPPORTED_FIELDS
        if unsupported:
            raise ConnectorFailure(
                ErrorCategory.INVALID_CONFIG,
                "static web allowlist contains unsupported fields",
                details={"unsupported_fields": sorted(unsupported)},
            )
        required = {"source_record_id", "url", "text", "quality_status"}
        if not required.issubset(self.spec.field_allowlist):
            raise ConnectorFailure(
                ErrorCategory.INVALID_CONFIG,
                "static web allowlist must include source_record_id, url, text and quality_status",
            )
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
                    checked_at=_utc_now(),
                    detail_code=exc.category.value,
                )
        return HealthReport(
            source_id=self.spec.id,
            status=HealthStatus.UNHEALTHY if self._closed else self._last_status,
            checked_at=_utc_now(),
            detail_code="closed" if self._closed else self._last_detail,
        )

    async def collect(self, request: CollectRequest) -> CollectBatch:
        if not self._validated:
            await self.validate()
        checkpoint = self._checkpoint_value(request.checkpoint)
        offset = int(checkpoint["offset"])
        limit = min(request.limit, self.spec.limits.max_records_per_run)
        selected_urls = self.policy.start_urls[offset : offset + limit]
        if not selected_urls:
            offset = 0
            selected_urls = self.policy.start_urls[:limit]

        result = await self._run_worker(selected_urls, dict(checkpoint["validators"]))
        failures = result.get("failures")
        pages = result.get("pages")
        if not isinstance(failures, list) or not isinstance(pages, list):
            raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "static web worker returned an invalid result")

        missing_urls = {
            str(failure.get("url"))
            for failure in failures
            if isinstance(failure, dict) and failure.get("kind") == "not_found" and failure.get("url")
        }
        critical_failures = [
            failure
            for failure in failures
            if not isinstance(failure, dict) or failure.get("kind") != "not_found"
        ]
        if critical_failures:
            self._raise_worker_failure(critical_failures[0])

        collected_at = _utc_now()
        records: list[RecordEnvelope] = []
        present_urls: set[str] = set()
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("url"), str):
                raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "static web worker page is invalid")
            url = page["url"]
            if url not in selected_urls:
                raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "worker returned a URL outside this job")
            present_urls.add(url)
            payload = self._payload(page)
            archive_sha256: str | None = None
            if self.archive is not None:
                receipt = self.archive.store(
                    source_id=self.spec.id,
                    request_url=url,
                    status_code=200,
                    headers={str(key): str(value) for key, value in dict(page.get("headers", {})).items()},
                    body={
                        "sanitized_html": str(page.get("sanitized_html", "")),
                        "structure_fingerprint": str(page.get("structure_fingerprint", "")),
                        "record_links": [
                            {
                                "source_record_id": self._source_record_id(url),
                                "raw_hash": self._payload_raw_hash(payload),
                            }
                        ],
                    },
                    collected_at=collected_at,
                )
                archive_sha256 = receipt.sha256
            records.append(self._envelope(url, payload, collected_at, archive_sha256))

        missing_counts = {str(key): int(value) for key, value in checkpoint["missing_counts"].items()}
        pending_deletions = {str(value) for value in checkpoint["pending_deletions"]}
        validators = dict(checkpoint["validators"])
        for url in present_urls:
            identity = self._source_record_id(url)
            missing_counts.pop(identity, None)
            pending_deletions.discard(identity)
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get("url"), str):
                headers = page.get("headers", {})
                if isinstance(headers, dict):
                    validators[page["url"]] = {
                        "etag": str(headers.get("etag") or ""),
                        "last_modified": str(headers.get("last-modified") or ""),
                    }
        for url in missing_urls:
            identity = self._source_record_id(url)
            count = missing_counts.get(identity, 0) + 1
            if count >= 2:
                pending_deletions.add(identity)
                missing_counts.pop(identity, None)
            else:
                missing_counts[identity] = count

        end = offset + len(selected_urls)
        has_more = end < len(self.policy.start_urls)
        next_offset = end if has_more else 0
        next_checkpoint = Checkpoint(
            source_id=self.spec.id,
            value={
                "policy_hash": self._policy_hash(),
                "offset": next_offset,
                "missing_counts": missing_counts,
                "pending_deletions": sorted(pending_deletions),
                "validators": validators,
                "cycle_complete": not has_more,
            },
            version=1,
            updated_at=collected_at,
        )
        stats = result.get("stats", {})
        self._last_status = HealthStatus.HEALTHY
        self._last_detail = "cache_hit" if isinstance(stats, dict) and stats.get("httpcache_hit") else "ok"
        return CollectBatch(records=tuple(records), next_checkpoint=next_checkpoint, has_more=has_more)

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        if checkpoint is None:
            return DeleteBatch(source_id=self.spec.id)
        if checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint belongs to another source")
        pending = checkpoint.value.get("pending_deletions", [])
        if not isinstance(pending, list) or any(not isinstance(value, str) for value in pending):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint has invalid pending deletions")
        value = dict(checkpoint.value)
        value["pending_deletions"] = []
        next_checkpoint = Checkpoint(
            source_id=checkpoint.source_id,
            value=value,
            version=checkpoint.version,
            updated_at=_utc_now(),
        )
        return DeleteBatch(
            source_id=self.spec.id,
            source_record_ids=tuple(sorted(set(pending))),
            next_checkpoint=next_checkpoint,
        )

    async def close(self) -> None:
        self._closed = True

    async def _run_worker(self, urls: tuple[str, ...], validators: dict[str, Any] | None = None) -> dict[str, Any]:
        jobs = self.workspace / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex
        job_path = (jobs / f"{job_id}.job.json").resolve()
        result_path = (jobs / f"{job_id}.result.json").resolve()
        if self.workspace not in job_path.parents or self.workspace not in result_path.parents:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "worker path escaped workspace")
        job = {
            "job_schema_version": "1.0",
            "start_urls": list(urls),
            "allowed_url_prefixes": list(self.policy.allowed_url_prefixes),
            "required_selectors": list(self.policy.required_selectors),
            "snapshot_selector": self.spec.extractor.selector,
            "min_text_chars": self.policy.min_text_chars,
            "max_response_bytes": self.policy.max_response_bytes,
            "allow_private_network": self.policy.allow_private_network,
            "requests_per_minute": self.spec.limits.requests_per_minute,
            "timeout_seconds": self.spec.limits.timeout_seconds,
            "max_attempts": self.spec.limits.max_attempts,
            "validators": validators or {},
            "cache_directory": str((self.workspace / "httpcache").resolve()),
        }
        temporary = job_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, job_path)
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[2])
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source_root if not existing_pythonpath else f"{source_root}{os.pathsep}{existing_pythonpath}"
        environment["PYTHONUTF8"] = "1"
        timeout = min(
            900,
            max(30, self.spec.limits.timeout_seconds * self.spec.limits.max_attempts * max(1, len(urls)) + 15),
        )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "omnisignal.connectors.static_web_worker",
                "--job",
                str(job_path),
                "--result",
                str(result_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            try:
                _, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "static web worker exceeded its time budget") from exc
            if process.returncode != 0 or not result_path.exists():
                raise ConnectorFailure(
                    ErrorCategory.RUNNER_CRASH,
                    "static web worker stopped without a valid result",
                    details={"return_code": process.returncode},
                )
            try:
                document = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "static web worker result is unreadable") from exc
            if not isinstance(document, dict) or document.get("worker_schema_version") != "1.0":
                raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "static web worker schema is unsupported")
            return document
        finally:
            temporary.unlink(missing_ok=True)
            job_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)

    def _checkpoint_value(self, checkpoint: Checkpoint | None) -> dict[str, Any]:
        if checkpoint is None:
            return {"offset": 0, "missing_counts": {}, "pending_deletions": [], "validators": {}}
        if checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint belongs to another source")
        if checkpoint.value.get("policy_hash") != self._policy_hash():
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint policy does not match connector policy")
        offset = checkpoint.value.get("offset")
        missing_counts = checkpoint.value.get("missing_counts", {})
        pending_deletions = checkpoint.value.get("pending_deletions", [])
        validators = checkpoint.value.get("validators", {})
        if (
            not isinstance(offset, int)
            or not isinstance(missing_counts, dict)
            or not isinstance(pending_deletions, list)
            or not isinstance(validators, dict)
        ):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint is invalid")
        return {
            "offset": offset,
            "missing_counts": missing_counts,
            "pending_deletions": pending_deletions,
            "validators": validators,
        }

    def _payload(self, page: dict[str, Any]) -> dict[str, Any]:
        available = {
            "url": page["url"],
            "published_at": page.get("published_at"),
            "title": page.get("title"),
            "text": page.get("text"),
            "quality_status": "accepted",
        }
        allowed = set(self.spec.field_allowlist) - {"source_record_id"}
        return {key: value for key, value in available.items() if key in allowed}

    def _envelope(
        self,
        url: str,
        payload: dict[str, Any],
        collected_at: datetime,
        archive_sha256: str | None = None,
    ) -> RecordEnvelope:
        return RecordEnvelope(
            source_id=self.spec.id,
            source_record_id=self._source_record_id(url),
            collected_at=collected_at,
            payload=payload,
            raw_hash=self._payload_raw_hash(payload),
            schema_version=self.spec.output_schema_version,
            permission=self.approval.permission,
            raw_archive_sha256=archive_sha256,
        )

    def _raise_worker_failure(self, failure: Any) -> None:
        if not isinstance(failure, dict):
            raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "static web worker failure is invalid")
        kind = failure.get("kind")
        details = {"failure_kind": kind, "status": failure.get("status")}
        if kind == "robots_forbidden":
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "robots.txt forbids this collection", details=details)
        if kind in {"access_barrier", "redirect_blocked"}:
            raise ConnectorFailure(ErrorCategory.PERMISSION, "web access barrier stopped collection", details=details)
        if kind == "rate_limit":
            retry_after = failure.get("retry_after")
            seconds = int(retry_after) if isinstance(retry_after, str) and retry_after.isdigit() else 60
            raise ConnectorFailure(
                ErrorCategory.RATE_LIMIT,
                "web source rate limit stopped collection",
                retry_after_seconds=seconds,
                details=details,
            )
        if kind in {"schema_drift", "non_html"}:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "web page structure no longer matches contract", details=details)
        if kind in {"upstream", "request_failed"}:
            raise ConnectorFailure(ErrorCategory.TRANSIENT_UPSTREAM, "web source remained unavailable after retries", details=details)
        raise ConnectorFailure(ErrorCategory.UNKNOWN, "web source returned an unexpected result", details=details)

    def _policy_hash(self) -> str:
        serialized = json.dumps(self.policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _source_record_id(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    @staticmethod
    def _payload_raw_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
