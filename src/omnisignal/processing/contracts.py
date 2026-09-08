"""Strict configuration and wire models for optional processing plugins."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnisignal.contracts.connector import PROHIBITED_FIELD_NAMES


_ID_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
_FIELD_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
_ENTRYPOINT_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*$"
SAFE_INPUT_FIELDS = frozenset(
    {"canonical_url", "title", "text", "published_at", "updated_at", "language", "entity_ids"}
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PluginManifest(StrictModel):
    sdk_version: str = Field(pattern=r"^1\.\d+$")
    plugin_id: str = Field(pattern=_ID_PATTERN)
    plugin_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    entrypoint: str = Field(pattern=_ENTRYPOINT_PATTERN)
    runner_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_schema_version: str = Field(pattern=r"^1\.\d+$")
    input_fields: tuple[str, ...] = Field(min_length=1, max_length=len(SAFE_INPUT_FIELDS))
    output_fields: tuple[str, ...] = Field(min_length=1, max_length=100)
    output_types: dict[
        str, Literal["string", "integer", "number", "boolean", "string_array", "object"]
    ]
    accepted_quality_statuses: tuple[Literal["accepted", "warning", "quarantined"], ...] = (
        "accepted",
        "warning",
    )
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_batch_records: int = Field(default=100, ge=1, le=1_000)
    max_output_bytes: int = Field(default=5_000_000, ge=1_000, le=50_000_000)
    enabled: bool = False
    approved: bool = False
    trust_level: Literal["reviewed_project_code"] = "reviewed_project_code"
    network_access: Literal[False] = False
    secret_refs: tuple[str, ...] = Field(default=(), max_length=0)
    options: dict[str, object] = Field(default_factory=dict)

    @field_validator("input_fields")
    @classmethod
    def validate_input_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or not set(value).issubset(SAFE_INPUT_FIELDS):
            raise ValueError("plugin input_fields contain duplicates or unsafe fields")
        return value

    @field_validator("output_fields")
    @classmethod
    def validate_output_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(re.fullmatch(_FIELD_PATTERN, field) is None for field in value):
            raise ValueError("plugin output_fields contain duplicates or invalid names")
        return value

    @model_validator(mode="after")
    def require_explicit_activation(self) -> "PluginManifest":
        if self.enabled and not self.approved:
            raise ValueError("enabled plugin must be explicitly approved")
        try:
            json.dumps(self.options, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("plugin options must be JSON serializable") from exc
        if set(self.output_types) != set(self.output_fields):
            raise ValueError("plugin output_types must define every declared output field exactly once")
        if _contains_prohibited_key(self.options):
            raise ValueError("plugin options contain a prohibited secret-like field")
        return self

    def manifest_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class PluginInputRecord(StrictModel):
    normalized_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str = Field(pattern=_ID_PATTERN)
    quality_status: Literal["accepted", "warning", "quarantined"]
    fields: dict[str, object]


class PluginOutputRecord(StrictModel):
    normalized_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict[str, object]
    quality_status: Literal["accepted", "quarantined"] = "accepted"
    quality_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def reject_sensitive_keys(self) -> "PluginOutputRecord":
        if _contains_prohibited_key(self.values):
            raise ValueError("plugin output contains a prohibited secret-like field")
        return self


class PluginJob(StrictModel):
    protocol_version: str = Field(pattern=r"^1\.\d+$")
    schema_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    plugin_id: str = Field(pattern=_ID_PATTERN)
    plugin_version: str
    entrypoint: str = Field(pattern=_ENTRYPOINT_PATTERN)
    output_schema_version: str
    output_fields: tuple[str, ...]
    output_types: dict[str, str]
    records: tuple[PluginInputRecord, ...]
    options: dict[str, object]


class PluginResult(StrictModel):
    protocol_version: str = Field(pattern=r"^1\.\d+$")
    schema_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    plugin_id: str = Field(pattern=_ID_PATTERN)
    plugin_version: str
    output_schema_version: str
    outputs: tuple[PluginOutputRecord, ...]


def load_plugin_manifest(path: Path) -> PluginManifest:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return PluginManifest.model_validate(document)


def output_value_matches_type(value: object, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string_array":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def _contains_prohibited_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).casefold()
            if lowered in PROHIBITED_FIELD_NAMES or any(
                marker in lowered for marker in ("token", "secret", "password", "cookie", "private_key")
            ):
                return True
            if _contains_prohibited_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_prohibited_key(item) for item in value)
    return False
