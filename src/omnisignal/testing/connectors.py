"""Deterministic in-memory connector used for contract and recovery tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from omnisignal.contracts import (
    Checkpoint,
    CollectBatch,
    CollectRequest,
    ConnectorSpec,
    DeleteBatch,
    HealthReport,
    HealthStatus,
    RecordEnvelope,
)
from omnisignal.contracts.errors import ConnectorFailure, ErrorCategory


class FixtureConnector:
    def __init__(
        self,
        spec: ConnectorSpec,
        records: list[dict[str, Any]],
        *,
        deleted_ids: tuple[str, ...] = (),
        permission: str = "project_owned_fixture",
    ) -> None:
        self.spec = spec
        self._records = records
        self._deleted_ids = deleted_ids
        self._permission = permission
        self._next_failure: ErrorCategory | None = None
        self._closed = False

    def fail_next(self, category: ErrorCategory) -> None:
        self._next_failure = category

    async def validate(self) -> None:
        if self._closed:
            raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "connector is closed")
        for index, item in enumerate(self._records):
            if "id" not in item:
                raise ConnectorFailure(
                    ErrorCategory.SCHEMA_DRIFT,
                    "fixture record is missing id",
                    details={"record_index": index},
                )

    async def health(self) -> HealthReport:
        return HealthReport(
            source_id=self.spec.id,
            status=HealthStatus.UNHEALTHY if self._closed else HealthStatus.HEALTHY,
            checked_at=datetime.now(timezone.utc),
            detail_code="closed" if self._closed else "fixture_ready",
        )

    async def collect(self, request: CollectRequest) -> CollectBatch:
        if self._next_failure is not None:
            category = self._next_failure
            self._next_failure = None
            raise ConnectorFailure(category, f"simulated {category.value}")
        if self._closed:
            raise ConnectorFailure(ErrorCategory.RUNNER_CRASH, "connector is closed")

        offset = 0
        if request.checkpoint is not None:
            if request.checkpoint.source_id != self.spec.id:
                raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "checkpoint source mismatch")
            offset = int(request.checkpoint.value.get("offset", 0))

        end = min(offset + request.limit, len(self._records))
        now = datetime.now(timezone.utc)
        envelopes = tuple(self._envelope(item, now) for item in self._records[offset:end])
        checkpoint = Checkpoint(
            source_id=self.spec.id,
            value={"offset": end},
            version=1,
            updated_at=now,
        )
        return CollectBatch(records=envelopes, next_checkpoint=checkpoint, has_more=end < len(self._records))

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        del checkpoint
        return DeleteBatch(source_id=self.spec.id, source_record_ids=self._deleted_ids)

    async def close(self) -> None:
        self._closed = True

    def _envelope(self, item: dict[str, Any], collected_at: datetime) -> RecordEnvelope:
        canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return RecordEnvelope(
            source_id=self.spec.id,
            source_record_id=str(item["id"]),
            collected_at=collected_at,
            payload=item,
            raw_hash=hashlib.sha256(canonical).hexdigest(),
            schema_version=self.spec.output_schema_version,
            permission=self._permission,
        )
