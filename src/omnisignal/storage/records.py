"""Database operations for idempotent connector record persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from omnisignal.contracts import RecordEnvelope

from .models import IngestedRecord


@dataclass(frozen=True, slots=True)
class UpsertSummary:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def upsert_records(session: Session, records: Iterable[RecordEnvelope]) -> UpsertSummary:
    inserted = 0
    updated = 0
    unchanged = 0

    for record in records:
        identity = (record.source_id, record.source_record_id)
        stored = session.get(IngestedRecord, identity)
        if stored is None:
            session.add(
                IngestedRecord(
                    source_id=record.source_id,
                    source_record_id=record.source_record_id,
                    payload=record.payload,
                    raw_hash=record.raw_hash,
                    schema_version=record.schema_version,
                    permission=record.permission,
                    raw_archive_sha256=record.raw_archive_sha256,
                    first_seen_at=record.collected_at,
                    last_seen_at=record.collected_at,
                )
            )
            inserted += 1
            continue

        changed = (
            stored.raw_hash != record.raw_hash
            or stored.schema_version != record.schema_version
            or stored.permission != record.permission
            or stored.raw_archive_sha256 != record.raw_archive_sha256
            or stored.deleted_at is not None
        )
        stored.last_seen_at = record.collected_at
        stored.deleted_at = None
        if changed:
            stored.payload = record.payload
            stored.raw_hash = record.raw_hash
            stored.schema_version = record.schema_version
            stored.permission = record.permission
            stored.raw_archive_sha256 = record.raw_archive_sha256
            updated += 1
        else:
            unchanged += 1

    session.flush()
    return UpsertSummary(inserted=inserted, updated=updated, unchanged=unchanged)


def mark_records_deleted(
    session: Session,
    source_id: str,
    source_record_ids: Iterable[str],
    *,
    deleted_at: datetime | None = None,
) -> int:
    timestamp = deleted_at or datetime.now(timezone.utc)
    changed = 0
    for source_record_id in source_record_ids:
        stored = session.get(IngestedRecord, (source_id, source_record_id))
        if stored is not None and stored.deleted_at is None:
            stored.deleted_at = timestamp
            changed += 1
    session.flush()
    return changed
