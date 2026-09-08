"""Workspace-level verification and promotion gates for reverse-lab evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError
import yaml

from .contracts import (
    FindingRecord,
    PromotionDecision,
    PromotionReview,
    SampleManifest,
    TargetAuthorization,
    TargetRegistry,
    TargetStatus,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


class LabGateError(RuntimeError):
    """Raised when a reverse-lab action fails closed."""


@dataclass(frozen=True)
class VerifiedSample:
    sample_id: str
    target_id: str
    relative_path: str
    sha256: str


def _load_yaml(path: Path, model_type: type[ModelT]) -> ModelT:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model_type.model_validate(raw)
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        raise LabGateError(f"invalid {model_type.__name__}: {path.name}") from exc


def _resolve_inside(root: Path, relative_path: str, required_prefix: str) -> Path:
    normalized = relative_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[0] != required_prefix or ".." in parts:
        raise LabGateError(f"path must stay under {required_prefix}/")
    candidate = (root / Path(*parts)).resolve()
    expected_root = (root / required_prefix).resolve()
    try:
        candidate.relative_to(expected_root)
    except ValueError as exc:
        raise LabGateError(f"path escaped {required_prefix}/") from exc
    return candidate


class LabWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.registry_path = self.root / "allowlist.yaml"
        self.audit_path = self.root / "audit" / "operations.jsonl"

    def load_registry(self) -> TargetRegistry:
        return _load_yaml(self.registry_path, TargetRegistry)

    def _record_path(self, path: Path, directory: str) -> Path:
        candidate = path.resolve() if path.is_absolute() else (self.root / path).resolve()
        expected_root = (self.root / directory).resolve()
        try:
            candidate.relative_to(expected_root)
        except ValueError as exc:
            raise LabGateError(f"record path must stay under {directory}/") from exc
        if not candidate.is_file():
            raise LabGateError(f"record file does not exist under {directory}/")
        return candidate

    def _target(self, target_id: str, today: date) -> TargetAuthorization:
        registry = self.load_registry()
        target = next((item for item in registry.targets if item.target_id == target_id), None)
        if target is None:
            raise LabGateError("target is not present in the allowlist")
        if target.status != TargetStatus.ALLOWED:
            raise LabGateError("target is not allowed")
        if target.valid_from > today:
            raise LabGateError("target authorization is not active yet")
        if target.expires_on is not None and target.expires_on < today:
            raise LabGateError("target authorization has expired")
        return target

    def _audit(self, *, actor: str, action: str, outcome: str, detail: dict[str, str]) -> None:
        if not actor.strip() or len(actor) > 120:
            raise LabGateError("actor is required for audit")
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "outcome": outcome,
            "detail": detail,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def verify_sample(
        self,
        manifest_path: Path,
        *,
        actor: str,
        today: date | None = None,
    ) -> VerifiedSample:
        manifest = _load_yaml(self._record_path(manifest_path, "manifests"), SampleManifest)
        try:
            target = self._target(manifest.target_id, today or date.today())
            sample_path = _resolve_inside(self.root, manifest.relative_path, "samples")
            if not sample_path.is_file():
                raise LabGateError("registered sample file does not exist")
            actual_hash = hashlib.sha256(sample_path.read_bytes()).hexdigest()
            if actual_hash != manifest.sha256:
                raise LabGateError("sample hash does not match the manifest")
            if manifest.contains_secrets:
                raise LabGateError("sample marked as containing secrets is rejected")
            if manifest.contains_real_user_data and not target.allows_real_user_data:
                raise LabGateError("sample contains real user data outside the authorization")
            verified = VerifiedSample(
                sample_id=manifest.sample_id,
                target_id=manifest.target_id,
                relative_path=manifest.relative_path,
                sha256=actual_hash,
            )
            self._audit(
                actor=actor,
                action="verify_sample",
                outcome="allowed",
                detail={"sample_id": manifest.sample_id, "target_id": manifest.target_id, "sha256": actual_hash},
            )
            return verified
        except LabGateError as exc:
            self._audit(
                actor=actor,
                action="verify_sample",
                outcome="rejected",
                detail={"sample_id": manifest.sample_id, "target_id": manifest.target_id, "reason": str(exc)},
            )
            raise

    def approve_promotion(
        self,
        *,
        manifest_path: Path,
        finding_path: Path,
        review_path: Path,
        actor: str,
        today: date | None = None,
    ) -> PromotionReview:
        verified = self.verify_sample(manifest_path, actor=actor, today=today)
        finding = _load_yaml(self._record_path(finding_path, "findings"), FindingRecord)
        review = _load_yaml(self._record_path(review_path, "reviews"), PromotionReview)
        try:
            target = self._target(verified.target_id, today or date.today())
            if finding.sample_id != verified.sample_id or finding.target_id != verified.target_id:
                raise LabGateError("finding does not belong to the verified sample")
            if finding.tool not in target.allowed_tools:
                raise LabGateError("finding tool is not allowed for this target")
            if finding.contains_secrets or finding.contains_real_user_data:
                raise LabGateError("finding contains restricted material and cannot be promoted")
            if review.finding_id != finding.finding_id:
                raise LabGateError("review does not belong to this finding")
            if review.decision != PromotionDecision.APPROVED:
                raise LabGateError("promotion review is not approved")
            if not set(review.target_scope).issubset(set(target.authorized_scope)):
                raise LabGateError("promotion target scope exceeds the authorization")
            if not set(finding.proposed_connector_targets).issubset(set(review.target_scope)):
                raise LabGateError("finding proposes targets outside the reviewed scope")
            self._audit(
                actor=actor,
                action="approve_promotion",
                outcome="allowed",
                detail={
                    "finding_id": finding.finding_id,
                    "review_id": review.review_id,
                    "connector_id": review.connector_id,
                },
            )
            return review
        except LabGateError as exc:
            self._audit(
                actor=actor,
                action="approve_promotion",
                outcome="rejected",
                detail={"finding_id": finding.finding_id, "review_id": review.review_id, "reason": str(exc)},
            )
            raise
