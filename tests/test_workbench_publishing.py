"""Synthetic local publication tests. No production keys or network are used."""
from copy import deepcopy
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image
import pytest

from omnisignal.knowledge.preparation import validate_editorial_case
from omnisignal.knowledge.release import (
    KnowledgeError, digest, materialize_release, verify_release,
)
from omnisignal.knowledge.replica import KnowledgeReplica
from omnisignal.workbench.publishing import publish_assets
from omnisignal.workbench.taxonomy import bind_reviewed_taxonomy


@pytest.fixture
def publication(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "synthetic-signing.pem"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_KEY_ID", "synthetic-key")
    image = tmp_path / "synthetic.png"
    Image.new("RGB", (640, 480), "red").save(image)
    asset = {
        "id": "synthetic-1", "title": "合成广告样张", "brand": "Synthetic",
        "source_url": "https://example.invalid/campaign",
        "source_image_url": "https://example.invalid/image.png",
        "description": "人工编写的说明，未经模型验证。", "ocr_text": "SYNTHETIC",
        "reason": "试验审核理由不是图像事实", "purposes": ["brand", "conversion"],
        "status": "accepted", "permissions": {"display": "allowed", "analysis": "unknown", "export": "allowed"},
        "image_path": str(image), "width": 640, "height": 480, "revision": 1,
        "sha256": digest(image.read_bytes()), "project_id": "synthetic-project",
    }
    asset["reviewed_taxonomy"] = bind_reviewed_taxonomy(asset, {"tags": {"industry": ["food-beverage"], "medium": ["poster"], "task": ["campaign"]}, "note": "合成测试中明确给定的广告分类"}, "synthetic-reviewer", asset["revision"])
    return tmp_path / "published", asset, key


def test_signed_release_keeps_legacy_protocol_and_does_not_claim_delivery(publication):
    root, asset, key = publication
    before = deepcopy(asset)
    image_before = Path(asset["image_path"]).read_bytes()
    result = publish_assets(root, [asset], "synthetic-operator")
    assert result["status"] == "published"
    assert result["receipt_status"] == "unconfirmed"
    assert result["sequence"] == 1 and result["taxonomy_version"] == "nova-1"
    directory = root / "releases" / result["version"]
    manifest, records = verify_release(directory, {"synthetic-key": key.public_key()})
    assert digest((directory / "manifest.json").read_bytes()) == result["manifest_sha256"]
    assert manifest["schema_version"] == 1 and manifest["kind"] == "full"
    case = records[0]["case"]
    validate_editorial_case(case)
    assert case["model_version"] == "none"
    assert case["editorial"]["reason"] == asset["reason"]
    assert case["editorial"]["marketing_purposes"] == ["brand", "conversion"]
    assert "legacy_medium_fallback" not in case["editorial"]
    assert case["editorial"]["taxonomy_reviewed"] is True
    assert case["tags"] == asset["reviewed_taxonomy"]["tags"]
    assert not any(asset["reason"] in claim["text"] for claim in case["claims"])
    assert not any(asset["description"] in claim["text"] for claim in case["claims"])
    assert case["claims"][0]["text"] == "图片尺寸为 640 × 480 像素。"
    assert asset == before and Path(asset["image_path"]).read_bytes() == image_before
    assert not any("private" in path.name or path.suffix == ".pem" for path in root.rglob("*"))


def test_multiple_assets_and_later_delta_preserve_previous_cases(publication):
    root, first, key = publication
    second = {**deepcopy(first), "id": "synthetic-2", "title": "第二张合成广告"}
    second["reviewed_taxonomy"] = bind_reviewed_taxonomy(second, {k: first["reviewed_taxonomy"][k] for k in ("tags", "note")}, "reviewer", second["revision"])
    result = publish_assets(root, [first, second], "synthetic-operator")
    assert result["assets_count"] == 2
    second["revision"] = 2
    second["description"] = "人工补充的新说明"
    second["reviewed_taxonomy"] = bind_reviewed_taxonomy(second, {k: first["reviewed_taxonomy"][k] for k in ("tags", "note")}, "reviewer", second["revision"])
    result2 = publish_assets(root, [second], "synthetic-operator")
    manifest, _ = verify_release(root / "releases" / result2["version"], {"synthetic-key": key.public_key()})
    assert manifest["kind"] == "delta" and manifest["parent_release_id"] == result["version"]
    assert manifest["sequence"] == 2
    state = materialize_release(root)
    assert set(state) == {first["id"], second["id"]}
    assert state[second["id"]]["revision"] == 2


@pytest.mark.parametrize("permission", ["display", "export"])
@pytest.mark.parametrize("state", ["unknown", "denied", None])
def test_unknown_or_denied_permissions_block_entire_publication(publication, permission, state):
    root, asset, _ = publication
    invalid = {**deepcopy(asset), "id": "invalid"}
    invalid["permissions"][permission] = state
    with pytest.raises(KnowledgeError, match="display and export"):
        publish_assets(root, [asset, invalid], "operator")
    assert not (root / "latest.json").exists()
    assert not (root / "releases").exists()


def test_missing_signing_key_never_generates_one(publication, monkeypatch):
    root, asset, _ = publication
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE")
    with pytest.raises(KnowledgeError, match="not configured"):
        publish_assets(root, [asset], "operator")
    assert not root.exists()


@pytest.mark.parametrize("change", [
    {"status": "pending"}, {"source_url": "file:///private/file"},
    {"source_url": "https://secret:credential@example.invalid/campaign"},
    {"sha256": "0" * 64}, {"width": 100}, {"purposes": ["promotion"]},
    {"revision": 0}, {"id": "../escape"},
])
def test_invalid_or_changed_asset_is_not_published(publication, change):
    root, asset, _ = publication
    asset.update(change)
    with pytest.raises(KnowledgeError):
        publish_assets(root, [asset], "operator")
    assert not (root / "latest.json").exists()


def test_same_revision_changed_annotation_is_rejected(publication):
    root, asset, _ = publication
    publish_assets(root, [asset], "operator")
    asset["description"] = "变更说明需要提升版本"
    with pytest.raises(KnowledgeError, match="revision conflict"):
        publish_assets(root, [asset], "operator")
    assert len(list((root / "releases").iterdir())) == 1
    assert materialize_release(root)[asset["id"]]["revision"] == 1


def test_missing_optional_key_id_uses_public_fingerprint(publication, monkeypatch):
    root, asset, key = publication
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_KEY_ID")
    result = publish_assets(root, [asset], "operator")
    assert result["key_id"].startswith("ed25519-")
    verify_release(root / "releases" / result["version"], {result["key_id"]: key.public_key()})


def test_private_key_under_publication_root_is_rejected(publication, monkeypatch):
    root, asset, key = publication
    root.mkdir()
    private = root / "invalid-private-key.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", str(private))
    with pytest.raises(KnowledgeError, match="configuration is invalid"):
        publish_assets(root, [asset], "operator")
    assert not (root / "latest.json").exists()


def test_duplicate_asset_selection_does_not_publish(publication):
    root, asset, _ = publication
    with pytest.raises(KnowledgeError, match="duplicate asset"):
        publish_assets(root, [asset, deepcopy(asset)], "operator")
    assert not root.exists()


def test_changed_image_bytes_do_not_modify_existing_release(publication):
    root, asset, _ = publication
    result = publish_assets(root, [asset], "operator")
    manifest = root / "releases" / result["version"] / "manifest.json"
    before = manifest.read_bytes()
    Image.new("RGB", (640, 480), "blue").save(asset["image_path"])
    with pytest.raises(KnowledgeError, match="hash changed"):
        publish_assets(root, [asset], "operator")
    assert manifest.read_bytes() == before
    assert len(list((root / "releases").iterdir())) == 1


def test_consumer_reference_can_verify_and_activate_without_new_protocol(publication, tmp_path):
    root, asset, key = publication
    result = publish_assets(root, [asset], "operator")
    replica = KnowledgeReplica(tmp_path / "replica")
    # The consumer's existing signature verification accepts the generated files;
    # the adapter itself never invokes receiver activation or fabricates a receipt.
    manifest, records = verify_release(root / "releases" / result["version"], {"synthetic-key": key.public_key()})
    assert manifest["taxonomy_version"] == "nova-1"
    assert records[0]["case"]["case_id"] == asset["id"]
    accepted = replica.accept_release(root / "releases" / result["version"], {"synthetic-key": key.public_key()})
    assert accepted["status"] == "accepted"
    assert replica.query_cases()[0]["case_id"] == asset["id"]
    assert replica.asset_path(asset["id"], "image-1").read_bytes() == Path(asset["image_path"]).read_bytes()
