"""Framework-neutral connector protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnisignal.contracts import Checkpoint, CollectBatch, CollectRequest, ConnectorSpec, DeleteBatch, HealthReport


@runtime_checkable
class Connector(Protocol):
    spec: ConnectorSpec

    async def validate(self) -> None:
        """Validate local configuration without contacting unapproved targets."""

    async def health(self) -> HealthReport:
        """Return a sanitized connector health report."""

    async def collect(self, request: CollectRequest) -> CollectBatch:
        """Collect one bounded batch and return the next durable checkpoint."""

    async def sync_deletions(self, checkpoint: Checkpoint | None = None) -> DeleteBatch:
        """Return source-side deletions without silently dropping them."""

    async def close(self) -> None:
        """Release connector-owned resources."""
