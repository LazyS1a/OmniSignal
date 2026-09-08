"""Minimal durable state required before real connectors are allowed."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"
    QUARANTINED = "quarantined"


class ConnectorState(Base):
    __tablename__ = "connector_states"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkpoint: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SourceControlState(Base):
    """Operational source override, independent from registry authorization and checkpoints."""

    __tablename__ = "source_control_states"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SourceControlCommand(Base):
    """Idempotent result of one authenticated source-control request."""

    __tablename__ = "source_control_commands"

    command_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    response_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CollectionJob(Base):
    """Persisted lifecycle for one operator-requested allowlisted collection."""

    __tablename__ = "collection_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    result_summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestedRecord(Base):
    """Latest durable representation of one source-side record."""

    __tablename__ = "ingested_records"
    __table_args__ = (
        UniqueConstraint("source_id", "source_record_id", name="uq_record_source_identity"),
    )

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    raw_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(16))
    permission: Mapped[str] = mapped_column(String(128))
    raw_archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NormalizationRun(Base):
    """One deterministic source snapshot processed with one frozen configuration."""

    __tablename__ = "normalization_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    normalizer_version: Mapped[str] = mapped_column(String(32))
    input_set_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    input_count: Mapped[int] = mapped_column(Integer, default=0)
    normalized_count: Mapped[int] = mapped_column(Integer, default=0)
    unconfigured_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_group_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NormalizedRecord(Base):
    """Immutable normalized version of one exact ingested-record content hash."""

    __tablename__ = "normalized_records"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "source_record_id",
            "input_raw_hash",
            "normalizer_version",
            "config_hash",
            name="uq_normalized_input_version",
        ),
    )

    normalized_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    source_record_id: Mapped[str] = mapped_column(String(256))
    input_raw_hash: Mapped[str] = mapped_column(String(64))
    raw_archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission: Mapped[str] = mapped_column(String(128))
    source_schema_version: Mapped[str] = mapped_column(String(16))
    normalizer_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    normalized_hash: Mapped[str] = mapped_column(String(64))
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str] = mapped_column(String(32), default="und")
    entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    exact_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simhash64: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parent_source_record_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(20))
    quality_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NormalizationRunMembership(Base):
    """Exact disposition of every ingested input considered by one normalization run."""

    __tablename__ = "normalization_run_memberships"

    normalization_run_id: Mapped[str] = mapped_column(
        ForeignKey("normalization_runs.run_id"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_record_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    input_raw_hash: Mapped[str] = mapped_column(String(64))
    normalized_id: Mapped[str | None] = mapped_column(
        ForeignKey("normalized_records.normalized_id"), nullable=True, index=True
    )
    disposition: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QualityEvent(Base):
    __tablename__ = "quality_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalization_run_id: Mapped[str] = mapped_column(ForeignKey("normalization_runs.run_id"), index=True)
    normalized_id: Mapped[str] = mapped_column(ForeignKey("normalized_records.normalized_id"), index=True)
    code: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20))
    field: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContextEdge(Base):
    __tablename__ = "context_edges"

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalization_run_id: Mapped[str] = mapped_column(ForeignKey("normalization_runs.run_id"), index=True)
    from_normalized_id: Mapped[str] = mapped_column(ForeignKey("normalized_records.normalized_id"), index=True)
    to_normalized_id: Mapped[str] = mapped_column(ForeignKey("normalized_records.normalized_id"), index=True)
    relation: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalization_run_id: Mapped[str] = mapped_column(ForeignKey("normalization_runs.run_id"), index=True)
    duplicate_kind: Mapped[str] = mapped_column(String(20))
    member_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DuplicateMembership(Base):
    __tablename__ = "duplicate_memberships"

    group_id: Mapped[str] = mapped_column(ForeignKey("duplicate_groups.group_id"), primary_key=True)
    normalized_id: Mapped[str] = mapped_column(ForeignKey("normalized_records.normalized_id"), primary_key=True)


class PluginRun(Base):
    """One observable attempt of one deterministic logical plugin execution."""

    __tablename__ = "plugin_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    execution_id: Mapped[str] = mapped_column(String(64), index=True)
    plugin_id: Mapped[str] = mapped_column(String(64), index=True)
    plugin_version: Mapped[str] = mapped_column(String(32))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    output_schema_version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), index=True)
    records_seen: Mapped[int] = mapped_column(Integer, default=0)
    records_sent: Mapped[int] = mapped_column(Integer, default=0)
    records_skipped: Mapped[int] = mapped_column(Integer, default=0)
    output_count: Mapped[int] = mapped_column(Integer, default=0)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PluginOutput(Base):
    __tablename__ = "plugin_outputs"

    run_id: Mapped[str] = mapped_column(ForeignKey("plugin_runs.run_id"), primary_key=True)
    normalized_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_records.normalized_id"), primary_key=True, index=True
    )
    values: Mapped[dict[str, object]] = mapped_column(JSON)
    quality_status: Mapped[str] = mapped_column(String(20))
    quality_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (UniqueConstraint("source_id", "idempotency_key", name="uq_run_source_idempotency"),)

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, native_enum=False), default=RunStatus.PENDING)
    records_seen: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("ingestion_runs.run_id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
