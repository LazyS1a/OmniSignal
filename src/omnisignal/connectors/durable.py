"""Framework-neutral durable execution for any Connector implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from omnisignal.contracts import CollectRequest, ConnectorFailure, ErrorCategory
from omnisignal.sdk import Connector
from omnisignal.storage import (
    AuditEvent,
    Base,
    ConnectorState,
    IngestionRun,
    RunStatus,
    assert_source_enabled,
    create_database_engine,
    mark_records_deleted,
    upsert_records,
)


@dataclass(frozen=True, slots=True)
class DurableRunSummary:
    run_id: str
    source_id: str
    batches: int
    records_seen: int
    inserted: int
    updated: int
    unchanged: int
    tombstoned: int
    cycle_complete: bool
    database_target: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_connector_durably(
    connector: Connector,
    *,
    database_url: str,
    database_target: str,
    initialize_schema: bool,
    inter_batch_delay_seconds: float = 0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    run_id: str | None = None,
) -> DurableRunSummary:
    engine = create_database_engine(database_url)
    if initialize_schema:
        Base.metadata.create_all(engine)

    run_id = run_id or str(uuid4())
    batches = seen = inserted = updated = unchanged = tombstoned = 0
    checkpoint = None
    try:
        with Session(engine) as session:
            assert_source_enabled(session, connector.spec.id)
            run = IngestionRun(
                run_id=run_id,
                source_id=connector.spec.id,
                idempotency_key=run_id,
                status=RunStatus.RUNNING,
                started_at=_utc_now(),
            )
            session.add(run)
            session.commit()
            state = session.get(ConnectorState, connector.spec.id)
            if state is not None:
                from omnisignal.contracts import Checkpoint

                checkpoint = Checkpoint(
                    source_id=state.source_id,
                    value=state.checkpoint,
                    version=state.checkpoint_version,
                    updated_at=state.updated_at,
                )

            try:
                while batches < connector.spec.pagination.max_pages:
                    batch = await connector.collect(
                        CollectRequest(
                            run_id=run_id,
                            limit=connector.spec.pagination.page_size,
                            checkpoint=checkpoint,
                        )
                    )
                    upsert = upsert_records(session, batch.records)
                    deletions = await connector.sync_deletions(batch.next_checkpoint)
                    checkpoint = deletions.next_checkpoint or batch.next_checkpoint
                    deleted = mark_records_deleted(session, connector.spec.id, deletions.source_record_ids)
                    state = session.get(ConnectorState, connector.spec.id)
                    if state is None:
                        state = ConnectorState(source_id=connector.spec.id)
                        session.add(state)
                    state.checkpoint = checkpoint.value
                    state.checkpoint_version = checkpoint.version
                    state.updated_at = checkpoint.updated_at

                    batches += 1
                    seen += len(batch.records)
                    inserted += upsert.inserted
                    updated += upsert.updated
                    unchanged += upsert.unchanged
                    tombstoned += deleted
                    run.records_seen = seen
                    run.records_written = inserted + updated + tombstoned
                    session.commit()
                    if not batch.has_more:
                        break
                    if inter_batch_delay_seconds:
                        await sleep(inter_batch_delay_seconds)

                run.status = RunStatus.SUCCEEDED
                run.finished_at = _utc_now()
                session.add(
                    AuditEvent(
                        run_id=run_id,
                        actor="durable_connector_runner",
                        action="connector_run_completed",
                        target=connector.spec.id,
                        detail={
                            "batches": batches,
                            "records_seen": seen,
                            "records_written": run.records_written,
                            "tombstoned": tombstoned,
                        },
                    )
                )
                session.commit()
            except (Exception, asyncio.CancelledError) as exc:
                failure = exc if isinstance(exc, ConnectorFailure) else ConnectorFailure(
                    ErrorCategory.RUNNER_CRASH, "connector execution interrupted"
                )
                session.rollback()
                failed_run = session.get(IngestionRun, run_id)
                if failed_run is not None:
                    should_pause = failure.category in {
                        ErrorCategory.AUTHENTICATION,
                        ErrorCategory.PERMISSION,
                        ErrorCategory.RATE_LIMIT,
                        ErrorCategory.POLICY_VIOLATION,
                        ErrorCategory.RESOURCE_EXHAUSTED,
                        ErrorCategory.SCHEMA_DRIFT,
                    }
                    failed_run.status = RunStatus.PAUSED if should_pause else RunStatus.FAILED
                    failed_run.error_code = failure.category.value
                    # Connector messages may contain upstream bodies or credentials.
                    # Persist the stable category only; never arbitrary exception text.
                    failed_run.error_summary = f"connector stopped: {failure.category.value}"
                    failed_run.finished_at = _utc_now()
                    session.add(
                        AuditEvent(
                            run_id=run_id,
                            actor="durable_connector_runner",
                            action="connector_run_stopped",
                            target=connector.spec.id,
                            detail={
                                "category": failure.category.value,
                                "action": "pause" if should_pause else failure.action.value,
                                "retry_after_seconds": failure.retry_after_seconds,
                            },
                        )
                    )
                    session.commit()
                raise
    finally:
        try:
            await connector.close()
        finally:
            engine.dispose()

    value = checkpoint.value if checkpoint is not None else {}
    return DurableRunSummary(
        run_id=run_id,
        source_id=connector.spec.id,
        batches=batches,
        records_seen=seen,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        tombstoned=tombstoned,
        cycle_complete=bool(value.get("cycle_complete", False)),
        database_target=database_target,
    )
