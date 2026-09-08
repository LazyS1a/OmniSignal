"""Data contracts for the authorized reverse-engineering laboratory.

The lab is deliberately separate from production connectors. These models
contain authorization and evidence metadata only; credentials and captured
payloads are not valid fields.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ID_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetStatus(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class AuthorizationBasis(StrEnum):
    SELF_OWNED = "self_owned"
    OPEN_SOURCE = "open_source"
    WRITTEN_AUTHORIZATION = "written_authorization"


class ReverseTool(StrEnum):
    GHIDRA = "ghidra"
    JADX = "jadx"
    APKTOOL = "apktool"
    FRIDA = "frida"
    MITMPROXY = "mitmproxy"


class TargetAuthorization(StrictModel):
    target_id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=120)
    status: TargetStatus
    authorization_basis: AuthorizationBasis
    authorization_reference: str = Field(min_length=1, max_length=240)
    authorized_scope: tuple[str, ...] = Field(min_length=1)
    allowed_tools: frozenset[ReverseTool] = Field(min_length=1)
    valid_from: date
    expires_on: date | None = None
    allows_real_user_data: bool = False
    owner: str = Field(min_length=1, max_length=120)

    @field_validator("authorization_reference", "owner")
    @classmethod
    def reject_secret_like_values(cls, value: str) -> str:
        if re.search(r"(?i)(?:token|secret|password|cookie|private[_ -]?key)\s*[:=]", value):
            raise ValueError("authorization metadata cannot contain secret-like values")
        return value

    @field_validator("authorized_scope")
    @classmethod
    def validate_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item or len(item) > 160 for item in normalized):
            raise ValueError("authorized_scope entries must be non-empty and at most 160 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("authorized_scope contains duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_validity_window(self) -> "TargetAuthorization":
        if self.expires_on is not None and self.expires_on < self.valid_from:
            raise ValueError("expires_on cannot precede valid_from")
        return self


class TargetRegistry(StrictModel):
    registry_version: int = Field(default=1, ge=1)
    updated_at: date
    targets: tuple[TargetAuthorization, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_targets(self) -> "TargetRegistry":
        target_ids = [target.target_id for target in self.targets]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("target registry contains duplicate target_id values")
        return self


class SampleManifest(StrictModel):
    manifest_version: str = Field(default="1.0", pattern=r"^1\.\d+$")
    sample_id: str = Field(pattern=ID_PATTERN)
    target_id: str = Field(pattern=ID_PATTERN)
    relative_path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=SHA256_PATTERN)
    source_reference: str = Field(min_length=1, max_length=240)
    acquired_at: date
    purpose: str = Field(min_length=1, max_length=240)
    contains_real_user_data: bool = False
    contains_secrets: bool = False

    @field_validator("relative_path")
    @classmethod
    def require_safe_sample_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith(("/", "//")) or re.match(r"^[a-zA-Z]:/", normalized):
            raise ValueError("sample path must be relative")
        parts = [part for part in normalized.split("/") if part]
        if not parts or parts[0] != "samples" or ".." in parts:
            raise ValueError("sample path must stay under samples/")
        return "/".join(parts)


class FindingRecord(StrictModel):
    finding_version: str = Field(default="1.0", pattern=r"^1\.\d+$")
    finding_id: str = Field(pattern=ID_PATTERN)
    sample_id: str = Field(pattern=ID_PATTERN)
    target_id: str = Field(pattern=ID_PATTERN)
    tool: ReverseTool
    tool_version: str = Field(min_length=1, max_length=64)
    created_at: datetime
    summary: str = Field(min_length=1, max_length=1000)
    artifact_paths: tuple[str, ...] = ()
    proposed_connector_targets: tuple[str, ...] = Field(min_length=1)
    contains_real_user_data: bool = False
    contains_secrets: bool = False

    @field_validator("artifact_paths")
    @classmethod
    def require_safe_artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            normalized = item.replace("\\", "/")
            if not normalized.startswith("artifacts/") or ".." in normalized.split("/"):
                raise ValueError("finding artifacts must stay under artifacts/")
        return value


class PromotionDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PromotionReview(StrictModel):
    review_version: str = Field(default="1.0", pattern=r"^1\.\d+$")
    review_id: str = Field(pattern=ID_PATTERN)
    finding_id: str = Field(pattern=ID_PATTERN)
    connector_id: str = Field(pattern=ID_PATTERN)
    target_scope: tuple[str, ...] = Field(min_length=1)
    decision: PromotionDecision
    decision_reason: str = Field(min_length=1, max_length=500)
    authorization_confirmed: bool = False
    sample_verified: bool = False
    secret_scan_passed: bool = False
    user_data_reviewed: bool = False
    reviewer: str = Field(min_length=1, max_length=120)
    reviewed_at: datetime

    @model_validator(mode="after")
    def require_all_checks_for_approval(self) -> "PromotionReview":
        checks = (
            self.authorization_confirmed,
            self.sample_verified,
            self.secret_scan_passed,
            self.user_data_reviewed,
        )
        if self.decision == PromotionDecision.APPROVED and not all(checks):
            raise ValueError("approved promotion requires every review check to pass")
        return self
