from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

from pydantic import ValidationError
import pytest
import yaml

from omnisignal.reverse_lab import (
    LabGateError,
    LabWorkspace,
    PromotionReview,
    SampleManifest,
    TargetRegistry,
)
from omnisignal.reverse_lab.tool_installer import ToolInstallError, safe_extract_zip, sha256_file


PROJECT_ROOT = Path(__file__).parents[1]


def write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def build_workspace(tmp_path: Path, *, target_id: str = "fixture_target") -> tuple[LabWorkspace, Path, Path]:
    root = tmp_path / "reverse_lab"
    sample_path = root / "samples" / "fixture.bin"
    sample_path.parent.mkdir(parents=True)
    sample_path.write_bytes(b"owned fixture only\n")
    sample_hash = hashlib.sha256(sample_path.read_bytes()).hexdigest()
    write_yaml(
        root / "allowlist.yaml",
        {
            "registry_version": 1,
            "updated_at": "2026-09-03",
            "targets": [
                {
                    "target_id": target_id,
                    "display_name": "Owned fixture target",
                    "status": "allowed",
                    "authorization_basis": "self_owned",
                    "authorization_reference": "test fixture",
                    "authorized_scope": ["fixture-protocol-mapping"],
                    "allowed_tools": ["ghidra"],
                    "valid_from": "2026-01-01",
                    "expires_on": "2026-12-31",
                    "allows_real_user_data": False,
                    "owner": "test_suite",
                }
            ],
        },
    )
    manifest_path = root / "manifests" / "fixture.yaml"
    write_yaml(
        manifest_path,
        {
            "manifest_version": "1.0",
            "sample_id": "fixture_sample",
            "target_id": target_id,
            "relative_path": "samples/fixture.bin",
            "sha256": sample_hash,
            "source_reference": "test-owned sample",
            "acquired_at": "2026-09-03",
            "purpose": "gate regression",
            "contains_real_user_data": False,
            "contains_secrets": False,
        },
    )
    return LabWorkspace(root), manifest_path, sample_path


def test_repository_fixture_is_registered_and_hash_matches() -> None:
    lab_root = PROJECT_ROOT / "reverse_lab"
    manifest = SampleManifest.model_validate(
        yaml.safe_load((lab_root / "manifests" / "fixture_owned_client.yaml").read_text(encoding="utf-8"))
    )
    registry = TargetRegistry.model_validate(
        yaml.safe_load((lab_root / "allowlist.yaml").read_text(encoding="utf-8"))
    )
    actual_hash = hashlib.sha256((lab_root / manifest.relative_path).read_bytes()).hexdigest()
    assert any(target.target_id == manifest.target_id for target in registry.targets)
    assert actual_hash == manifest.sha256


def test_registered_owned_sample_is_allowed_and_audited(tmp_path: Path) -> None:
    workspace, manifest_path, _ = build_workspace(tmp_path)
    verified = workspace.verify_sample(manifest_path, actor="test_operator", today=date(2026, 9, 3))
    event = json.loads(workspace.audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert verified.target_id == "fixture_target"
    assert event["outcome"] == "allowed"
    assert event["detail"]["sha256"] == verified.sha256


def test_unlisted_target_is_rejected_and_audited(tmp_path: Path) -> None:
    workspace, manifest_path, _ = build_workspace(tmp_path, target_id="unlisted_target")
    registry = yaml.safe_load(workspace.registry_path.read_text(encoding="utf-8"))
    registry["targets"][0]["target_id"] = "different_target"
    write_yaml(workspace.registry_path, registry)
    with pytest.raises(LabGateError, match="not present"):
        workspace.verify_sample(manifest_path, actor="test_operator", today=date(2026, 9, 3))
    event = json.loads(workspace.audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert event["outcome"] == "rejected"


def test_tampered_sample_is_rejected(tmp_path: Path) -> None:
    workspace, manifest_path, sample_path = build_workspace(tmp_path)
    sample_path.write_bytes(b"tampered\n")
    with pytest.raises(LabGateError, match="hash"):
        workspace.verify_sample(manifest_path, actor="test_operator", today=date(2026, 9, 3))


def test_expired_authorization_is_rejected(tmp_path: Path) -> None:
    workspace, manifest_path, _ = build_workspace(tmp_path)
    with pytest.raises(LabGateError, match="expired"):
        workspace.verify_sample(manifest_path, actor="test_operator", today=date(2027, 1, 1))


def test_sample_paths_cannot_escape_workspace() -> None:
    with pytest.raises(ValidationError, match="samples"):
        SampleManifest.model_validate(
            {
                "sample_id": "fixture_sample",
                "target_id": "fixture_target",
                "relative_path": "samples/../private.txt",
                "sha256": "0" * 64,
                "source_reference": "fixture",
                "acquired_at": "2026-09-03",
                "purpose": "path test",
            }
        )


def test_manifest_record_must_stay_inside_workspace(tmp_path: Path) -> None:
    workspace, _, _ = build_workspace(tmp_path)
    outside = tmp_path / "outside.yaml"
    write_yaml(outside, {"manifest_version": "1.0"})
    with pytest.raises(LabGateError, match="manifests"):
        workspace.verify_sample(outside, actor="test_operator", today=date(2026, 9, 3))


def test_approved_review_requires_every_check() -> None:
    with pytest.raises(ValidationError, match="every review check"):
        PromotionReview.model_validate(
            {
                "review_id": "fixture_review",
                "finding_id": "fixture_finding",
                "connector_id": "fixture_connector",
                "target_scope": ["fixture-protocol-mapping"],
                "decision": "approved",
                "decision_reason": "All fixture checks passed",
                "authorization_confirmed": True,
                "sample_verified": True,
                "secret_scan_passed": False,
                "user_data_reviewed": True,
                "reviewer": "reviewer",
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def test_promotion_is_allowed_only_inside_reviewed_scope(tmp_path: Path) -> None:
    workspace, manifest_path, _ = build_workspace(tmp_path)
    finding_path = workspace.root / "findings" / "fixture.yaml"
    review_path = workspace.root / "reviews" / "fixture.yaml"
    now = datetime.now(timezone.utc).isoformat()
    write_yaml(
        finding_path,
        {
            "finding_id": "fixture_finding",
            "sample_id": "fixture_sample",
            "target_id": "fixture_target",
            "tool": "ghidra",
            "tool_version": "12.1.3",
            "created_at": now,
            "summary": "Synthetic endpoint mapping",
            "artifact_paths": ["artifacts/fixture-map.json"],
            "proposed_connector_targets": ["fixture-protocol-mapping"],
            "contains_real_user_data": False,
            "contains_secrets": False,
        },
    )
    write_yaml(
        review_path,
        {
            "review_id": "fixture_review",
            "finding_id": "fixture_finding",
            "connector_id": "fixture_connector",
            "target_scope": ["fixture-protocol-mapping"],
            "decision": "approved",
            "decision_reason": "Approved only for the synthetic fixture scope",
            "authorization_confirmed": True,
            "sample_verified": True,
            "secret_scan_passed": True,
            "user_data_reviewed": True,
            "reviewer": "test_reviewer",
            "reviewed_at": now,
        },
    )
    review = workspace.approve_promotion(
        manifest_path=manifest_path,
        finding_path=finding_path,
        review_path=review_path,
        actor="test_reviewer",
        today=date(2026, 9, 3),
    )
    assert review.connector_id == "fixture_connector"


def test_rejected_review_cannot_be_promoted(tmp_path: Path) -> None:
    workspace, manifest_path, _ = build_workspace(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    finding_path = workspace.root / "findings" / "fixture.yaml"
    review_path = workspace.root / "reviews" / "fixture.yaml"
    write_yaml(
        finding_path,
        {
            "finding_id": "fixture_finding",
            "sample_id": "fixture_sample",
            "target_id": "fixture_target",
            "tool": "ghidra",
            "tool_version": "12.1.3",
            "created_at": now,
            "summary": "Synthetic finding",
            "artifact_paths": [],
            "proposed_connector_targets": ["fixture-protocol-mapping"],
        },
    )
    write_yaml(
        review_path,
        {
            "review_id": "fixture_rejection",
            "finding_id": "fixture_finding",
            "connector_id": "fixture_connector",
            "target_scope": ["fixture-protocol-mapping"],
            "decision": "rejected",
            "decision_reason": "Training evidence is not a production connector source",
            "authorization_confirmed": True,
            "sample_verified": True,
            "secret_scan_passed": True,
            "user_data_reviewed": True,
            "reviewer": "test_reviewer",
            "reviewed_at": now,
        },
    )
    with pytest.raises(LabGateError, match="not approved"):
        workspace.approve_promotion(
            manifest_path=manifest_path,
            finding_path=finding_path,
            review_path=review_path,
            actor="test_reviewer",
            today=date(2026, 9, 3),
        )


def test_tool_lock_reuses_official_github_projects() -> None:
    lock = yaml.safe_load((PROJECT_ROOT / "reverse_lab" / "tools.lock.yaml").read_text(encoding="utf-8"))
    assert lock["source_policy"] == "official_github_releases_only"
    assert set(lock["tools"]) == {"ghidra", "jadx", "apktool", "frida", "mitmproxy"}
    for tool in lock["tools"].values():
        assert tool["repository"].startswith("https://github.com/")


def test_reverse_lab_compose_is_offline_and_separate_from_production() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.reverse-lab.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["lab-gate"]
    assert service["network_mode"] == "none"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert "environment" not in service
    assert "depends_on" not in service


def test_production_build_context_excludes_reverse_lab() -> None:
    ignored = {
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "reverse_lab" in ignored


def test_jadx_runner_requires_path_redaction_receipt() -> None:
    runner = (PROJECT_ROOT / "scripts" / "run-jadx-lab.ps1").read_text(encoding="utf-8")
    assert "receipt_version = '1.1'" in runner
    assert "path_redaction = 'lab_root'" in runner
    assert "<LAB_ROOT>" in runner


def test_tool_installer_hashes_files_and_rejects_zip_traversal(tmp_path: Path) -> None:
    asset = tmp_path / "asset.bin"
    asset.write_bytes(b"official fixture asset")
    assert sha256_file(asset) == hashlib.sha256(asset.read_bytes()).hexdigest()

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "blocked")
    with pytest.raises(ToolInstallError, match="traversal"):
        safe_extract_zip(archive, tmp_path / "extract")
