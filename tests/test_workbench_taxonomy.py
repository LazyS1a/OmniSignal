"""Explicit taxonomy review: synthetic files, isolated accounts, no model calls."""
from copy import deepcopy
import io
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from omnisignal.knowledge.release import KnowledgeError, verify_release
from omnisignal.knowledge.taxonomy import TAXONOMY, TAXONOMY_VERSION
from omnisignal.workbench.app import create_app
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.publishing import publish_assets
from omnisignal.workbench.store import Conflict, Store
from omnisignal.workbench.taxonomy import fresh_taxonomy, taxonomy_status

BASE = "/workbench/api"


def content():
    return {"tags": {"industry": ["food-beverage"], "medium": ["packaging"], "task": ["series"]},
            "note": "人工核对的合成包装分类，仅用于测试"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_DIR", raising=False)
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_USERS_FILE", raising=False)
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "synthetic-key.pem"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_KEY_ID", "synthetic-key")
    app = create_app(tmp_path / "store", start_worker=False)
    store = app.state.store
    image = io.BytesIO()
    Image.new("RGB", (160, 120), "blue").save(image, "PNG")
    item = store.import_asset({"title": "自有合成分类样张", "source_url": "https://example.invalid/own"}, image.getvalue())
    item = store.review(item["id"], {"expected_revision": item["revision"], "status": "accepted",
        "kind": "reference", "purposes": [], "reason": "作者自制样张进行离线测试",
        "permissions": {"display": "allowed", "analysis": "unknown", "export": "allowed"},
        "permission_note": "仅自有合成样张用于验收"}, "synthetic-admin", "admin")
    return app, item, key


def classify(store, item, payload=None, role="operator"):
    return store.review(item["id"], {"expected_revision": item["revision"],
                        "reviewed_taxonomy": content() if payload is None else payload}, "synthetic-reviewer", role)


def publish(store, item):
    asset = store.asset(item["id"])
    asset["image_path"] = str(store.images / asset["image_file"])
    return publish_assets(store.root / "published", [asset], "synthetic-publisher")


def test_legacy_asset_remains_readable_without_fabricated_classification(setup):
    app, item, _ = setup
    store = Store(app.state.store.root)
    old = store.asset(item["id"])
    assert old["reviewed_taxonomy_status"] == "none"
    assert "reviewed_taxonomy" not in old and "tags" not in old
    assert store.list_assets()["items"][0]["reviewed_taxonomy_status"] == "none"
    with pytest.raises(KnowledgeError, match="人工分类审核"):
        publish(store, old)
    assert not (store.root / "published" / "latest.json").exists()


def test_classification_only_review_binds_operator_and_exact_revision_without_permission_change(setup):
    app, item, _ = setup
    store = app.state.store
    classified = classify(store, item)
    review = classified["reviewed_taxonomy"]
    assert classified["reviewed_taxonomy_status"] == "current"
    assert classified["reviewed_analysis_status"] == "none"
    assert review["tags"] == content()["tags"] and review["note"] == content()["note"]
    assert review["revision"] == classified["revision"] == item["revision"] + 1
    assert review["asset_id"] == item["id"] and review["sha256"] == item["sha256"]
    assert review["reviewed_by"] == "synthetic-reviewer" and review["reviewed_at"]
    assert review["taxonomy_version"] == TAXONOMY_VERSION
    assert classified["permissions"] == item["permissions"]
    assert Store(store.root).asset(item["id"])["reviewed_taxonomy"] == review
    with store.db() as db:
        audit = db.execute("SELECT actor,details FROM audit WHERE action='taxonomy_review'").fetchone()
        assert audit["actor"] == "synthetic-reviewer" and json.loads(audit["details"])["tags"] == review["tags"]


def test_reviewed_tags_and_evidence_survive_signed_publication(setup):
    app, item, key = setup
    store = app.state.store
    classified = classify(store, item)
    published = publish(store, classified)
    manifest, records = verify_release(store.root / "published" / "releases" / published["version"],
                                       {"synthetic-key": key.public_key()})
    case = records[0]["case"]
    assert manifest["taxonomy_version"] == "nova-1" and manifest["schema_version"] == 1
    assert case["tags"] == content()["tags"]
    assert case["editorial"]["taxonomy_reviewed"] is True
    assert case["editorial"]["reviewed_taxonomy"] == classified["reviewed_taxonomy"]
    assert case["editorial"]["material_kind"] == "reference"
    assert "legacy_medium_fallback" not in case["editorial"] and "digital" not in case["tags"]["medium"]


def test_ordinary_edit_requires_explicit_reapproval_before_new_publication(setup):
    app, item, _ = setup
    store = app.state.store
    classified = classify(store, item)
    changed = store.review(item["id"], {"expected_revision": classified["revision"], "reason": "再次核对原始来源"}, "editor", "operator")
    assert changed["reviewed_taxonomy_status"] == "stale"
    assert store.list_assets()["items"][0]["reviewed_taxonomy_status"] == "stale"
    assert changed["reviewed_taxonomy"] == classified["reviewed_taxonomy"]
    with pytest.raises(KnowledgeError, match="分类审核已过期"):
        publish(store, changed)
    reapproved = classify(store, changed)
    assert reapproved["reviewed_taxonomy"]["revision"] == changed["revision"] + 1
    assert publish(store, reapproved)["status"] == "published"


INVALID = [
    None, [], {},
    {"tags": {"industry": ["food-beverage"], "medium": ["packaging"]}, "note": ""},
    {"tags": {"industry": [], "medium": ["packaging"], "task": []}, "note": ""},
    {"tags": {"industry": ["food-beverage"], "medium": [], "task": []}, "note": ""},
    {"tags": {"industry": ["食品饮料"], "medium": ["packaging"], "task": []}, "note": ""},
    {"tags": {"industry": ["food-beverage"], "medium": ["banner-ad"], "task": []}, "note": ""},
    {"tags": {"industry": ["food-beverage"], "medium": ["packaging"], "task": ["brand"]}, "note": ""},
    {"tags": {"industry": "food-beverage", "medium": ["packaging"], "task": []}, "note": ""},
    {"tags": {"industry": ["food-beverage", "food-beverage"], "medium": ["packaging"], "task": []}, "note": ""},
    {"tags": {"industry": ["other"], "medium": ["packaging"], "task": []}, "note": "  "},
    {"tags": {"industry": ["food-beverage"], "medium": ["packaging"], "task": [], "style": []}, "note": ""},
    {**content(), "reviewed_by": "spoofed-admin"},
    {**content(), "taxonomy_version": "nova-2"},
    {**content(), "note": 42}, {**content(), "note": "x" * 2001},
]


@pytest.mark.parametrize("invalid", INVALID)
def test_invalid_classification_cannot_commit_or_advance_revision(setup, invalid):
    app, item, _ = setup
    store = app.state.store
    before = store.asset(item["id"])
    with pytest.raises(Conflict):
        store.review(item["id"], {"expected_revision": item["revision"], "reviewed_taxonomy": invalid}, "reviewer", "operator")
    assert store.asset(item["id"]) == before


def test_other_is_explicit_with_note_and_task_can_remain_empty(setup):
    app, item, _ = setup
    payload = {"tags": {"industry": ["other"], "medium": ["spatial"], "task": []}, "note": "  合成测试行业在字典中未单列  "}
    classified = classify(app.state.store, item, payload)
    assert classified["reviewed_taxonomy"]["note"] == payload["note"].strip()
    assert classified["reviewed_taxonomy"]["tags"]["task"] == []


@pytest.mark.parametrize("field,value", [("asset_id", "another-case"), ("sha256", "0" * 64),
    ("revision", 1), ("taxonomy_version", "nova-2"), ("schema_version", 2), ("reviewed_by", "")])
def test_copied_or_inconsistent_saved_evidence_never_counts_as_current(setup, field, value):
    app, item, _ = setup
    classified = classify(app.state.store, item)
    tampered = deepcopy(classified)
    tampered["reviewed_taxonomy"][field] = value
    assert taxonomy_status(tampered) == "stale"
    with pytest.raises(Conflict):
        fresh_taxonomy(tampered)


def test_changed_image_and_stale_expected_revision_cannot_be_reapproved(setup):
    app, item, _ = setup
    store = app.state.store
    classified = classify(store, item)
    with pytest.raises(Conflict, match="素材已被修改"):
        classify(store, item)
    (store.images / classified["image_file"]).write_bytes(b"changed synthetic bytes")
    with pytest.raises(Conflict, match="图片内容已变化"):
        classify(store, classified)
    assert store.asset(item["id"])["revision"] == classified["revision"]


def test_authenticated_dictionary_and_review_roles_keep_existing_boundaries(setup):
    app, item, _ = setup
    hashed = password_hash("synthetic-password")
    users = [{"username": role, "role": role, "password_hash": hashed} for role in ("operator", "viewer")]
    (app.state.store.root / "users.json").write_text(json.dumps(users), encoding="utf-8")
    assert TestClient(app).get(BASE + "/taxonomy").status_code == 401
    for role in ("viewer", "operator"):
        web = TestClient(app)
        login = web.post(BASE + "/session", json={"username": role, "password": "synthetic-password"})
        web.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        result = web.get(BASE + "/taxonomy")
        assert result.status_code == 200 and result.json() == {"version": TAXONOMY_VERSION, "groups": TAXONOMY}
        payload = {"expected_revision": item["revision"], "reviewed_taxonomy": content()}
        result = web.post(BASE + f"/assets/{item['id']}/review", json=payload)
        if role == "viewer":
            assert result.status_code == 403
        else:
            assert result.status_code == 200
            assert result.json()["reviewed_taxonomy"]["reviewed_by"] == "operator"
            denied = web.post(BASE + f"/assets/{item['id']}/review", json={**payload,
                "expected_revision": result.json()["revision"], "permissions": item["permissions"]})
            assert denied.status_code == 409 and "管理员" in denied.json()["detail"]
