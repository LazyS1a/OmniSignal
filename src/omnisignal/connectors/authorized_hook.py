"""Process-isolated adapter for explicitly approved Hook/private-interface workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omnisignal.contracts import (
    AuthKind,
    Capability,
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
    RiskLevel,
)
from omnisignal.contracts.connector import PROHIBITED_FIELD_NAMES
from omnisignal.governance import HookSourceApproval
from omnisignal.runtime import IsolatedWorkerError, WorkerErrorKind, run_json_worker

from .archive import FileRawResponseArchive
from .authorized_hook_protocol import OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION
from .authorized_hook_policy import AuthorizedHookPolicy


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuthorizedHookConnector:
    def __init__(
        self,
        spec: ConnectorSpec,
        policy: AuthorizedHookPolicy,
        approval: HookSourceApproval,
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
        self._runner_path: Path | None = None
        self._last_detail = "configured"

    async def validate(self) -> None:
        if self._closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "authorized Hook connector is closed")
        if self.spec.kind != ConnectorKind.EXPERIMENTAL_AUTHORIZED or self.spec.risk_level != RiskLevel.HIGH:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "authorized Hook connector must remain high risk")
        if Capability.AUTHORIZED_HOOK not in self.spec.capabilities:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "authorized_hook capability is required")
        if self.spec.extractor.kind != ExtractorKind.PLUGIN:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "authorized Hook connector requires a plugin entrypoint")
        if self.spec.isolation.mode != IsolationMode.PROCESS or not self.spec.isolation.kill_switch:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "authorized Hook connector requires process isolation")
        if self.spec.limits.max_concurrency != 1:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "authorized Hook connector concurrency must be one")
        if self.spec.limits.max_attempts != 1:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "authorized Hook worker cannot auto-replay side effects")
        if len(self.spec.allowed_targets) != 1:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "authorized Hook connector requires exactly one target")
        if self.approval.source_id != self.spec.id or self.approval.runner_entrypoint != self.spec.extractor.entrypoint:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook approval does not match connector spec")
        if self.spec.limits.requests_per_minute > self.approval.rate_budget_rpm:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook connector exceeds approved request budget")
        if self.policy.protocol_version != PROTOCOL_VERSION:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "Hook protocol version is not supported by this core")
        if self.approval.output_schema_fingerprint != OUTPUT_SCHEMA_FINGERPRINT:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "registered Hook schema is not supported by this core")
        if "source_record_id" not in self.spec.field_allowlist:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "Hook output requires source_record_id")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._runner_path = self._resolve_runner()
        actual_hash = hashlib.sha256(self._runner_path.read_bytes()).hexdigest()
        if actual_hash != self.approval.runner_sha256:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "authorized Hook runner hash does not match registry")
        self._validated = True

    async def health(self) -> HealthReport:
        try:
            if not self._validated and not self._closed:
                await self.validate()
            circuit = self._load_circuit()
            status = HealthStatus.PAUSED if circuit["open"] else HealthStatus.HEALTHY
            detail = "circuit_open" if circuit["open"] else self._last_detail
        except ConnectorFailure as exc:
            status = HealthStatus.PAUSED
            detail = exc.category.value
        if self._closed:
            status, detail = HealthStatus.UNHEALTHY, "closed"
        return HealthReport(source_id=self.spec.id, status=status, checked_at=_utc_now(), detail_code=detail)

    async def collect(self, request: CollectRequest) -> CollectBatch:
        if not self._validated:
            await self.validate()
        if self.approval.authorization_expires_at <= _utc_now():
            raise ConnectorFailure(ErrorCategory.PERMISSION, "authorized Hook approval has expired")
        circuit = self._load_circuit()
        if circuit["open"]:
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "authorized Hook circuit is open")
        cursor = self._checkpoint_cursor(request.checkpoint)
        try:
            limit = min(request.limit, self.spec.limits.max_records_per_run)
            result = await self._run_worker(cursor, limit)
            batch = self._build_batch(result, limit)
        except ConnectorFailure as exc:
            self._record_failure(exc.category)
            raise
        self._record_success()
        return batch

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        del checkpoint
        return DeleteBatch(source_id=self.spec.id)

    async def close(self) -> None:
        self._closed = True

    def reset_circuit(self) -> None:
        self._write_circuit(0, False, "operator_reset")

    def _resolve_runner(self) -> Path:
        entrypoint = self.approval.runner_entrypoint
        module_name, separator, function_name = entrypoint.partition(":")
        if separator != ":" or function_name != "main" or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", module_name):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "Hook runner entrypoint must be a project module main")
        source_root = Path(__file__).resolve().parents[2]
        candidate = source_root.joinpath(*module_name.split(".")).with_suffix(".py").resolve()
        if source_root not in candidate.parents or not candidate.is_file():
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook runner must be a file inside project src")
        return candidate

    async def _run_worker(self, cursor: str | None, limit: int) -> dict[str, Any]:
        document = {
            "protocol_version": self.policy.protocol_version,
            "target": self.spec.allowed_targets[0],
            "cursor": cursor,
            "limit": limit,
            "runner_options": self.policy.runner_options,
            "requests_per_minute": self.spec.limits.requests_per_minute,
        }
        module_name = self.approval.runner_entrypoint.partition(":")[0]
        try:
            return await run_json_worker(
                module_name=module_name,
                document=document,
                workspace=self.workspace,
                timeout_seconds=self.spec.limits.timeout_seconds,
                max_output_bytes=self.policy.max_output_bytes,
                environment_overrides=self._credential_environment(),
            )
        except ValueError as exc:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, str(exc)) from exc
        except IsolatedWorkerError as exc:
            if exc.kind == WorkerErrorKind.OUTPUT_TOO_LARGE:
                raise ConnectorFailure(
                    ErrorCategory.RESOURCE_EXHAUSTED, "authorized Hook output exceeded size limit"
                ) from exc
            if exc.kind == WorkerErrorKind.INVALID_OUTPUT:
                raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "authorized Hook output is invalid") from exc
            raise ConnectorFailure(
                ErrorCategory.RUNNER_CRASH,
                "authorized Hook worker stopped",
                details={"failure_kind": exc.kind.value, "return_code": exc.return_code},
            ) from exc

    def _credential_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        if self.approval.credential_ref is not None:
            source_name = self.approval.credential_ref.removeprefix("env:")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", source_name):
                raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "Hook credential env reference is invalid")
            secret = os.environ.get(source_name)
            if not secret:
                raise ConnectorFailure(ErrorCategory.AUTHENTICATION, "authorized Hook credential is unavailable")
            environment["OMNISIGNAL_CONNECTOR_SECRET"] = secret
        return environment

    def _build_batch(self, result: dict[str, Any], requested_limit: int) -> CollectBatch:
        if _contains_prohibited_key(result):
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook output contains a prohibited field")
        allowed_top_level = {
            "protocol_version",
            "status",
            "target",
            "schema_fingerprint",
            "records",
            "next_cursor",
            "has_more",
            "error",
        }
        extra_top_level = set(result) - allowed_top_level
        if extra_top_level:
            raise ConnectorFailure(
                ErrorCategory.SCHEMA_DRIFT,
                "Hook output envelope contains unknown fields",
                details={"fields": sorted(str(field) for field in extra_top_level)},
            )
        if result.get("protocol_version") != self.policy.protocol_version:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook protocol version changed")
        if result.get("target") != self.spec.allowed_targets[0]:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook worker returned an unapproved target")
        if result.get("status") == "error":
            self._raise_worker_error(result.get("error"))
        if result.get("status") != "ok" or result.get("schema_fingerprint") != self.approval.output_schema_fingerprint:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook output schema fingerprint changed")
        records = result.get("records")
        has_more = result.get("has_more")
        next_cursor = result.get("next_cursor")
        if not isinstance(records, list) or not isinstance(has_more, bool):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook output envelope is invalid")
        if has_more and (not isinstance(next_cursor, str) or not next_cursor):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook pagination cursor is missing")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook pagination cursor is invalid")
        if len(records) > requested_limit:
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "Hook returned too many records")
        collected_at = _utc_now()
        archive_sha256: str | None = None
        checkpoint = Checkpoint(
            source_id=self.spec.id,
            value={
                "cursor": next_cursor,
                "permission": self.approval.permission,
                "runner_sha256": self.approval.runner_sha256,
                "output_schema_fingerprint": self.approval.output_schema_fingerprint,
                "cycle_complete": not has_more,
            },
            version=1,
            updated_at=collected_at,
        )
        if self.archive is not None:
            receipt = self.archive.store(
                source_id=self.spec.id,
                request_url=self.spec.allowed_targets[0],
                status_code=200,
                headers={"protocol-version": self.policy.protocol_version},
                body={
                    "records": records,
                    "record_links": [
                        {
                            "source_record_id": str(item["source_record_id"]),
                            "raw_hash": self._record_raw_hash(item),
                        }
                        for item in records
                    ],
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                },
                collected_at=collected_at,
            )
            archive_sha256 = receipt.sha256
        envelopes = tuple(self._record(item, collected_at, archive_sha256) for item in records)
        self._last_detail = "ok"
        return CollectBatch(records=envelopes, next_checkpoint=checkpoint, has_more=has_more)

    def _record(
        self,
        item: Any,
        collected_at: datetime,
        archive_sha256: str | None = None,
    ) -> RecordEnvelope:
        if not isinstance(item, dict) or _contains_prohibited_key(item):
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook record contains a prohibited field")
        extra = set(item) - set(self.spec.field_allowlist)
        if extra:
            raise ConnectorFailure(
                ErrorCategory.SCHEMA_DRIFT,
                "Hook record contains fields outside the connector allowlist",
                details={"fields": sorted(str(field) for field in extra)},
            )
        source_record_id = item.get("source_record_id")
        if not isinstance(source_record_id, str) or not source_record_id or len(source_record_id) > 512:
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook record identity is invalid")
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 1_000_000:
            raise ConnectorFailure(ErrorCategory.RESOURCE_EXHAUSTED, "Hook record exceeded 1 MB")
        payload = {key: value for key, value in item.items() if key != "source_record_id"}
        return RecordEnvelope(
            source_id=self.spec.id,
            source_record_id=source_record_id,
            collected_at=collected_at,
            payload=payload,
            raw_hash=self._record_raw_hash(item),
            schema_version=self.spec.output_schema_version,
            permission=self.approval.permission,
            raw_archive_sha256=archive_sha256,
        )

    @staticmethod
    def _record_raw_hash(item: dict[str, Any]) -> str:
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _checkpoint_cursor(self, checkpoint: Checkpoint | None) -> str | None:
        if checkpoint is None:
            return None
        if checkpoint.source_id != self.spec.id:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "Hook checkpoint belongs to another source")
        expected = {
            "permission": self.approval.permission,
            "runner_sha256": self.approval.runner_sha256,
            "output_schema_fingerprint": self.approval.output_schema_fingerprint,
        }
        if any(checkpoint.value.get(key) != value for key, value in expected.items()):
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook checkpoint approval or fingerprint changed")
        cursor = checkpoint.value.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "Hook checkpoint cursor is invalid")
        return cursor

    @staticmethod
    def _raise_worker_error(error: Any) -> None:
        if not isinstance(error, dict):
            raise ConnectorFailure(ErrorCategory.SCHEMA_DRIFT, "Hook worker error envelope is invalid")
        kind = error.get("kind")
        status = error.get("status")
        details = {"status": status, "kind": kind}
        if kind == "authentication" or status == 401:
            raise ConnectorFailure(ErrorCategory.AUTHENTICATION, "authorized Hook authentication failed", details=details)
        if kind == "permission" or status == 403:
            raise ConnectorFailure(ErrorCategory.PERMISSION, "authorized Hook permission was denied", details=details)
        if kind == "rate_limit" or status == 429:
            raise ConnectorFailure(ErrorCategory.RATE_LIMIT, "authorized Hook was rate limited", retry_after_seconds=60)
        raise ConnectorFailure(ErrorCategory.TRANSIENT_UPSTREAM, "authorized Hook upstream failed", details=details)

    def _load_circuit(self) -> dict[str, Any]:
        path = self.workspace / "circuit.json"
        if not path.exists():
            return {"consecutive_failures": 0, "open": False, "last_category": None}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook circuit state is unreadable") from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("consecutive_failures"), int)
            or not isinstance(value.get("open"), bool)
        ):
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook circuit state is invalid")
        return value

    def _record_failure(self, category: ErrorCategory) -> None:
        state = self._load_circuit()
        count = int(state["consecutive_failures"]) + 1
        immediate = category in {
            ErrorCategory.AUTHENTICATION,
            ErrorCategory.PERMISSION,
            ErrorCategory.POLICY_VIOLATION,
            ErrorCategory.SCHEMA_DRIFT,
        }
        self._write_circuit(count, immediate or count >= self.policy.circuit_failure_threshold, category.value)

    def _record_success(self) -> None:
        self._write_circuit(0, False, "success")

    def _write_circuit(self, count: int, opened: bool, category: str) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        path = (self.workspace / "circuit.json").resolve()
        if self.workspace not in path.parents:
            raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "Hook circuit path escaped workspace")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "consecutive_failures": count,
                    "open": opened,
                    "last_category": category,
                    "updated_at": _utc_now().isoformat(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _contains_prohibited_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in PROHIBITED_FIELD_NAMES or any(
                marker in lowered for marker in ("token", "secret", "password", "cookie", "private_key")
            ):
                return True
            if _contains_prohibited_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_prohibited_key(item) for item in value)
    return False
