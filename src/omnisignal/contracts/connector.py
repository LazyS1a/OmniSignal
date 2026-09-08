"""Connector configuration and data exchange contracts.

This module intentionally has no dependency on dlt, Scrapy, Playwright or
Prefect. Third-party frameworks must adapt to these models at the boundary.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROHIBITED_FIELD_NAMES = {
    "access_token",
    "government_id",
    "password",
    "payment_card",
    "precise_location",
    "private_key",
    "refresh_token",
    "session_cookie",
    "signing_secret",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectorKind(StrEnum):
    API = "api"
    RSS = "rss"
    HTML = "html"
    BROWSER = "browser"
    WEBSOCKET = "websocket"
    FILE = "file"
    EXPERIMENTAL_AUTHORIZED = "experimental_authorized"


class RiskLevel(StrEnum):
    STANDARD = "standard"
    ELEVATED = "elevated"
    HIGH = "high"


class Capability(StrEnum):
    INCREMENTAL = "incremental"
    REPLAY = "replay"
    DELETION_SYNC = "deletion_sync"
    BROWSER_RENDER = "browser_render"
    REALTIME = "realtime"
    AUTHORIZED_HOOK = "authorized_hook"


class AuthKind(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    OAUTH2 = "oauth2"
    COOKIE_SESSION = "cookie_session"


class AuthSpec(StrictModel):
    kind: AuthKind = AuthKind.NONE
    secret_ref: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.:/-]+$")

    @model_validator(mode="after")
    def validate_secret_reference(self) -> "AuthSpec":
        if self.kind == AuthKind.NONE and self.secret_ref is not None:
            raise ValueError("auth kind 'none' cannot have secret_ref")
        if self.kind != AuthKind.NONE and self.secret_ref is None:
            raise ValueError("authenticated connectors require secret_ref")
        return self


class PaginationKind(StrEnum):
    NONE = "none"
    PAGE = "page"
    OFFSET = "offset"
    CURSOR = "cursor"
    LINK_HEADER = "link_header"
    TIME_WINDOW = "time_window"
    INFINITE_SCROLL = "infinite_scroll"


class PaginationSpec(StrictModel):
    kind: PaginationKind = PaginationKind.NONE
    page_size: int = Field(default=100, ge=1, le=1000)
    cursor_field: str | None = None
    max_pages: int = Field(default=100, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_cursor(self) -> "PaginationSpec":
        if self.kind == PaginationKind.CURSOR and not self.cursor_field:
            raise ValueError("cursor pagination requires cursor_field")
        return self


class ExtractorKind(StrEnum):
    JSONPATH = "jsonpath"
    JMESPATH = "jmespath"
    CSS = "css"
    XPATH = "xpath"
    TRAFILATURA = "trafilatura"
    PLUGIN = "plugin"


class ExtractorSpec(StrictModel):
    kind: ExtractorKind
    selector: str | None = None
    entrypoint: str | None = Field(default=None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.:]*$")

    @model_validator(mode="after")
    def validate_extractor(self) -> "ExtractorSpec":
        if self.kind == ExtractorKind.PLUGIN and not self.entrypoint:
            raise ValueError("plugin extractor requires entrypoint")
        if self.kind in {ExtractorKind.JSONPATH, ExtractorKind.JMESPATH, ExtractorKind.CSS, ExtractorKind.XPATH} and not self.selector:
            raise ValueError(f"{self.kind.value} extractor requires selector")
        return self


class CheckpointStrategy(StrEnum):
    NONE = "none"
    CURSOR = "cursor"
    TIMESTAMP = "timestamp"
    OPAQUE = "opaque"


class CheckpointSpec(StrictModel):
    strategy: CheckpointStrategy = CheckpointStrategy.NONE
    commit_every: int = Field(default=100, ge=1, le=10_000)


class DeleteMode(StrEnum):
    NONE = "none"
    TOMBSTONE = "tombstone"
    POLL = "poll"
    WEBHOOK = "webhook"
    FULL_RESYNC = "full_resync"


class DeletionSpec(StrictModel):
    mode: DeleteMode = DeleteMode.NONE
    target_hours: int = Field(default=24, ge=1, le=720)


class IsolationMode(StrEnum):
    IN_PROCESS = "in_process"
    PROCESS = "process"
    CONTAINER = "container"


class IsolationSpec(StrictModel):
    mode: IsolationMode = IsolationMode.IN_PROCESS
    kill_switch: bool = True


class RuntimeLimits(StrictModel):
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    requests_per_minute: int = Field(default=30, ge=1, le=60_000)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    max_records_per_run: int = Field(default=10_000, ge=1, le=10_000_000)


class ConnectorSpec(StrictModel):
    spec_version: str = Field(pattern=r"^1\.\d+$")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    connector_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    kind: ConnectorKind
    risk_level: RiskLevel = RiskLevel.STANDARD
    capabilities: frozenset[Capability] = frozenset()
    allowed_targets: tuple[str, ...] = Field(min_length=1)
    field_allowlist: tuple[str, ...] = Field(min_length=1)
    output_schema_version: str = Field(default="1.0", pattern=r"^1\.\d+$")
    auth: AuthSpec = AuthSpec()
    pagination: PaginationSpec = PaginationSpec()
    extractor: ExtractorSpec
    checkpoint: CheckpointSpec = CheckpointSpec()
    deletion: DeletionSpec = DeletionSpec()
    isolation: IsolationSpec = IsolationSpec()
    limits: RuntimeLimits = RuntimeLimits()

    @field_validator("allowed_targets")
    @classmethod
    def validate_allowed_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_targets contains duplicates")
        for target in value:
            if "\n" in target or "\r" in target:
                raise ValueError("allowed_targets cannot contain line breaks")
            if target.lower().startswith("file://"):
                raise ValueError("file:// targets are not allowed")
            if re.search(r"://[^/]*@", target):
                raise ValueError("allowed_targets cannot contain inline credentials")
            if re.search(r"(?i)(?:token|secret|password|api[_-]?key)=", target):
                raise ValueError("allowed_targets cannot contain secret query parameters")
        return value

    @field_validator("field_allowlist")
    @classmethod
    def validate_field_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        lowered = {item.lower() for item in value}
        blocked = sorted(lowered & PROHIBITED_FIELD_NAMES)
        if blocked:
            raise ValueError(f"prohibited fields in allowlist: {blocked}")
        if len(lowered) != len(value):
            raise ValueError("field_allowlist contains duplicates")
        return value

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> "ConnectorSpec":
        if self.kind in {ConnectorKind.API, ConnectorKind.RSS, ConnectorKind.HTML, ConnectorKind.BROWSER}:
            if any(not target.lower().startswith(("https://", "http://")) for target in self.allowed_targets):
                raise ValueError(f"{self.kind.value} connectors require http(s) targets")
        if self.kind == ConnectorKind.WEBSOCKET:
            if any(not target.lower().startswith(("wss://", "ws://")) for target in self.allowed_targets):
                raise ValueError("websocket connectors require ws(s) targets")
        if self.kind == ConnectorKind.FILE:
            for target in self.allowed_targets:
                normalized = target.replace("\\", "/")
                if re.match(r"^[a-zA-Z]:/", normalized) or normalized.startswith("/"):
                    raise ValueError("file connector targets must be relative paths")
                if ".." in normalized.split("/"):
                    raise ValueError("file connector targets cannot traverse parent directories")
        if self.pagination.kind == PaginationKind.INFINITE_SCROLL and self.kind != ConnectorKind.BROWSER:
            raise ValueError("infinite_scroll is only valid for browser connectors")
        if self.kind == ConnectorKind.BROWSER and self.isolation.mode == IsolationMode.IN_PROCESS:
            raise ValueError("browser connectors must use process or container isolation")
        if self.kind == ConnectorKind.EXPERIMENTAL_AUTHORIZED:
            if self.risk_level != RiskLevel.HIGH:
                raise ValueError("authorized experimental connectors require high risk level")
            if self.isolation.mode == IsolationMode.IN_PROCESS:
                raise ValueError("authorized experimental connectors cannot run in process")
            if not self.isolation.kill_switch:
                raise ValueError("authorized experimental connectors require kill_switch")
            if Capability.AUTHORIZED_HOOK not in self.capabilities:
                raise ValueError("authorized experimental connectors require authorized_hook capability")
        return self


class Checkpoint(StrictModel):
    source_id: str
    value: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    updated_at: datetime


class CollectRequest(StrictModel):
    run_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{3,128}$")
    limit: int = Field(default=100, ge=1, le=10_000)
    checkpoint: Checkpoint | None = None


class RecordEnvelope(StrictModel):
    source_id: str
    source_record_id: str
    collected_at: datetime
    payload: dict[str, Any]
    raw_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_version: str = Field(default="1.0", pattern=r"^1\.\d+$")
    permission: str
    raw_archive_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CollectBatch(StrictModel):
    records: tuple[RecordEnvelope, ...]
    next_checkpoint: Checkpoint
    has_more: bool


class DeleteBatch(StrictModel):
    source_id: str
    source_record_ids: tuple[str, ...] = ()
    next_checkpoint: Checkpoint | None = None


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    PAUSED = "paused"


class HealthReport(StrictModel):
    source_id: str
    status: HealthStatus
    checked_at: datetime
    detail_code: str
