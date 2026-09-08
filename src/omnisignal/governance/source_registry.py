"""Fail-closed source-registry approval for connector execution."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from omnisignal.contracts import ConnectorFailure, ConnectorSpec, ErrorCategory


@dataclass(frozen=True, slots=True)
class SourceApproval:
    source_id: str
    permission: str
    authorization_basis: str
    rate_budget_rpm: int


@dataclass(frozen=True, slots=True)
class HookSourceApproval(SourceApproval):
    credential_ref: str | None
    runner_entrypoint: str
    runner_sha256: str
    output_schema_fingerprint: str
    authorization_expires_at: datetime


def load_source_approval(registry_path: Path, spec: ConnectorSpec) -> SourceApproval:
    """Return an immutable approval only when the registry fully authorizes a spec."""

    version, updated_at, entry = _load_registered_entry(registry_path, spec)
    authorization_basis, rate_budget = _validate_common_scope(entry, spec)
    if entry.get("credential_ref") is not None:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "static web source must not declare credentials")
    if entry.get("robots_policy") != "obey":
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source registry must require robots.txt compliance")
    return SourceApproval(
        source_id=spec.id,
        permission=_permission_reference(spec.id, version, updated_at, entry),
        authorization_basis=authorization_basis,
        rate_budget_rpm=rate_budget,
    )


def load_hook_source_approval(registry_path: Path, spec: ConnectorSpec) -> HookSourceApproval:
    """Authorize one high-risk worker without importing or executing its code."""

    version, updated_at, entry = _load_registered_entry(registry_path, spec)
    authorization_basis, rate_budget = _validate_common_scope(entry, spec)
    credential_ref = entry.get("credential_ref")
    if credential_ref is not None and (not isinstance(credential_ref, str) or not credential_ref.startswith("env:")):
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "hook credential_ref must be null or an env reference")
    if credential_ref != spec.auth.secret_ref:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "hook credential reference differs from source registry")
    runner_entrypoint = _required_text(entry, "runner_entrypoint")
    if runner_entrypoint != spec.extractor.entrypoint:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "hook runner entrypoint differs from source registry")
    runner_sha256 = _required_text(entry, "runner_sha256").lower()
    output_fingerprint = _required_text(entry, "output_schema_fingerprint").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", runner_sha256):
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "hook runner hash is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", output_fingerprint):
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "hook output schema fingerprint is invalid")
    _required_text(entry, "promotion_review_ref")
    expires_text = _required_text(entry, "authorization_expires_at")
    try:
        expires_at = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "hook authorization expiry is invalid") from exc
    if expires_at.tzinfo is None:
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "hook authorization expiry requires a timezone")
    if expires_at <= datetime.now(timezone.utc):
        raise ConnectorFailure(ErrorCategory.PERMISSION, "hook authorization has expired")
    return HookSourceApproval(
        source_id=spec.id,
        permission=_permission_reference(spec.id, version, updated_at, entry),
        authorization_basis=authorization_basis,
        rate_budget_rpm=rate_budget,
        credential_ref=credential_ref,
        runner_entrypoint=runner_entrypoint,
        runner_sha256=runner_sha256,
        output_schema_fingerprint=output_fingerprint,
        authorization_expires_at=expires_at,
    )


def _load_registered_entry(registry_path: Path, spec: ConnectorSpec) -> tuple[int, str, dict[str, Any]]:
    try:
        document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry is unreadable") from exc
    if not isinstance(document, dict):
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry must be a mapping")
    version = document.get("registry_version")
    updated_at = document.get("updated_at")
    sources = document.get("sources")
    if not isinstance(version, int) or version < 1 or not isinstance(updated_at, str) or not updated_at:
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry version metadata is invalid")
    if not isinstance(sources, list):
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry sources must be a list")
    matches = [entry for entry in sources if isinstance(entry, dict) and entry.get("id") == spec.id]
    if len(matches) != 1:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source is not uniquely registered")
    return version, updated_at, matches[0]


def _validate_common_scope(entry: dict[str, Any], spec: ConnectorSpec) -> tuple[str, int]:
    if entry.get("status") != "allowed":
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source registry has not enabled this source")
    if entry.get("connector_class") != spec.kind.value:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "connector kind differs from source registry")
    if entry.get("kill_switch") is not True or not spec.isolation.kill_switch:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "source kill switch is not enabled")
    authorization_basis = _required_text(entry, "authorization_basis")
    _required_text(entry, "owner")
    _required_text(entry, "terms_reference")
    _required_text(entry, "retention_profile")
    _required_text(entry, "deletion_mode")
    rate_budget = entry.get("rate_budget_rpm")
    if not isinstance(rate_budget, int) or isinstance(rate_budget, bool) or rate_budget < 1:
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry rate budget is invalid")
    if spec.limits.requests_per_minute > rate_budget:
        raise ConnectorFailure(ErrorCategory.POLICY_VIOLATION, "connector rate exceeds source registry budget")

    registry_fields = entry.get("field_allowlist")
    if not isinstance(registry_fields, list) or any(not isinstance(field, str) for field in registry_fields):
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry field allowlist is invalid")
    extra_fields = set(spec.field_allowlist) - set(registry_fields)
    if extra_fields:
        raise ConnectorFailure(
            ErrorCategory.POLICY_VIOLATION,
            "connector requests fields outside source registry",
            details={"fields": sorted(extra_fields)},
        )

    registry_targets = entry.get("allowed_targets")
    if not isinstance(registry_targets, list) or not registry_targets:
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, "source registry target allowlist is invalid")
    for target in spec.allowed_targets:
        if not any(isinstance(pattern, str) and _target_matches(target, pattern) for pattern in registry_targets):
            raise ConnectorFailure(
                ErrorCategory.POLICY_VIOLATION,
                "connector target is outside source registry",
                details={"target": target},
            )

    return authorization_basis, rate_budget


def _permission_reference(source_id: str, version: int, updated_at: str, entry: dict[str, Any]) -> str:
    entry_digest = hashlib.sha256(yaml.safe_dump(entry, allow_unicode=True, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"registry:{source_id}:v{version}:{updated_at}:{entry_digest}"


def _required_text(entry: dict[str, Any], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConnectorFailure(ErrorCategory.INVALID_CONFIG, f"source registry {field} is required")
    return value.strip()


def _target_matches(target: str, pattern: str) -> bool:
    if "*" in pattern[:-1] or pattern.count("*") > 1:
        return False
    wildcard = pattern.endswith("*")
    if wildcard and not pattern.endswith("/*"):
        return False
    base = pattern[:-1] if wildcard else pattern
    candidate_url = urlparse(target)
    registry_url = urlparse(base)
    if candidate_url.scheme not in {"http", "https"} or registry_url.scheme not in {"http", "https"}:
        return not wildcard and target == pattern and "/../" not in target and "@" not in target
    if (
        candidate_url.username
        or candidate_url.password
        or registry_url.username
        or registry_url.password
        or candidate_url.query
        or candidate_url.fragment
        or registry_url.query
        or registry_url.fragment
    ):
        return False
    candidate_port = candidate_url.port or (443 if candidate_url.scheme == "https" else 80)
    registry_port = registry_url.port or (443 if registry_url.scheme == "https" else 80)
    same_origin = (
        candidate_url.scheme.lower() == registry_url.scheme.lower()
        and (candidate_url.hostname or "").lower() == (registry_url.hostname or "").lower()
        and candidate_port == registry_port
    )
    if not same_origin:
        return False
    return candidate_url.path.startswith(registry_url.path) if wildcard else candidate_url.path == registry_url.path
