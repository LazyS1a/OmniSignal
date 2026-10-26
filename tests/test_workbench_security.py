"""Regression cases for session revocation, restore boundaries and withdrawals."""
import base64
import hashlib
import io
import json
from pathlib import Path
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from omnisignal.knowledge.release import KnowledgeError, verify_signature
from omnisignal.workbench.__main__ import backup, restore
from omnisignal.workbench.analysis import AnalysisService
from omnisignal.workbench.app import create_app
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.publishing import publish_assets, withdraw_assets
from omnisignal.workbench.store import Store
from omnisignal.workbench.taxonomy import bind_reviewed_taxonomy

BASE = "/workbench/api"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_USERS_FILE", raising=False)
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_DIR", raising=False)
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "synthetic-key.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", str(key_path))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_KEY_ID", "synthetic-key")
    app = create_app(tmp_path / "store", start_worker=False)
    hashed = password_hash("synthetic-password")
    users = [{"username": role, "role": role, "password_hash": hashed} for role in ("admin", "operator")]
    (app.state.store.root / "users.json").write_text(json.dumps(users), encoding="utf-8")
    web = TestClient(app)
    result = web.post(BASE + "/session", json={"username": "admin", "password": "synthetic-password"})
    assert result.status_code == 200
    web.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return app, web, key


def publish_sample(web):
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "red").save(output, "PNG")
    item = web.post(BASE + "/assets/import", json={"title": "合成测试广告", "source_url": "https://example.invalid/case",
                    "image_base64": base64.b64encode(output.getvalue()).decode()}).json()
    reviewed = web.post(BASE + f"/assets/{item['id']}/review", json={"expected_revision": item["revision"],
                        "status": "accepted", "kind": "advertisement", "purposes": ["brand"], "reason": "合成广告用途测试",
                        "permissions": {"display": "allowed", "analysis": "unknown", "export": "allowed"},
                        "permission_note": "合成图片自有测试",
                        "reviewed_taxonomy": {"tags": {"industry": ["culture"], "medium": ["poster"], "task": ["campaign"]}, "note": "合成测试活动海报分类"}})
    assert reviewed.status_code == 200, reviewed.text
    published = web.post(BASE + "/publish", json={"asset_ids": [item["id"]]})
    assert published.status_code == 200, published.text
    return web.get(BASE + f"/assets/{item['id']}").json()


def test_password_rotation_revokes_old_cookie_without_role_change(setup):
    app, web, _ = setup
    path = app.state.store.root / "users.json"
    users = json.loads(path.read_text())
    users[0]["password_hash"] = password_hash("synthetic-rotated-password")
    path.write_text(json.dumps(users), encoding="utf-8")
    assert web.get(BASE + "/summary").status_code == 401
    fresh = TestClient(app)
    assert fresh.post(BASE + "/session", json={"username": "admin", "password": "synthetic-password"}).status_code == 401
    assert fresh.post(BASE + "/session", json={"username": "admin", "password": "synthetic-rotated-password"}).status_code == 200


@pytest.mark.parametrize("payload", [
    '{"username":"admin","password":"synthetic-password","extra":NaN}',
    '{"username":"admin","username":"operator","password":"synthetic-password"}',
    '{"extra":' + '[' * 40 + '0' + ']' * 40 + '}',
    '{"extra":' + '[' * 1100 + '0' + ']' * 1100 + '}',
    '[]', 'not json',
])
def test_malformed_json_is_422_instead_of_server_error(setup, payload):
    _, web, _ = setup
    response = web.post(BASE + "/session", content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_published_asset_cannot_silently_revoke_local_permissions(setup):
    _, web, _ = setup
    item = publish_sample(web)
    for patch in ({"status": "excluded"}, {"status": "pending"},
                  {"permissions": {"display": "denied", "analysis": "unknown", "export": "allowed"}, "permission_note": "撤销展示"},
                  {"permissions": {"display": "allowed", "analysis": "unknown", "export": "unknown"}, "permission_note": "撤销导出"}):
        response = web.post(BASE + f"/assets/{item['id']}/review", json={"expected_revision": item["revision"], **patch})
        assert response.status_code == 409 and "永久撤回" in response.json()["detail"]
    unchanged = web.get(BASE + f"/assets/{item['id']}").json()
    assert unchanged["revision"] == item["revision"] and unchanged["status"] == "accepted"


def test_signed_withdrawal_is_permanent_and_receipt_is_not_fabricated(setup):
    app, web, key = setup
    item = publish_sample(web)
    response = web.post(BASE + "/withdraw", json={"asset_ids": [item["id"]], "reason": "合成案例永久撤回验收"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "withdrawn" and result["permanent"]
    assert result["receipt_status"] == "unconfirmed"
    directory = app.state.store.root / "published"
    signed = json.loads((directory / "withdrawals" / "1.json").read_text())
    verify_signature(signed, {"synthetic-key": key.public_key()})
    assert signed["case_ids"] == [item["id"]]
    current = web.get(BASE + f"/assets/{item['id']}").json()
    assert current["status"] == "excluded" and current["publication_status"] == "withdrawn"
    assert current["revision"] == item["revision"] + 1
    assert web.post(BASE + "/publish", json={"asset_ids": [item["id"]]}).status_code == 409
    # Even bypassing UI state cannot republish an original globally withdrawn ID.
    forged = app.state.store.asset(item["id"])
    forged.update(status="accepted", image_path=str(app.state.store.images / forged["image_file"]))
    forged["reviewed_taxonomy"] = bind_reviewed_taxonomy(forged,
        {k: forged["reviewed_taxonomy"][k] for k in ("tags", "note")}, "forged-reviewer", forged["revision"])
    with pytest.raises(KnowledgeError, match="withdrawn"):
        publish_assets(directory, [forged], "admin")


def test_missing_signer_does_not_change_editorial_state(setup, monkeypatch):
    _, web, _ = setup
    item = publish_sample(web)
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE")
    result = web.post(BASE + "/withdraw", json={"asset_ids": [item["id"]], "reason": "合成撤回"})
    assert result.status_code == 409
    current = web.get(BASE + f"/assets/{item['id']}").json()
    assert current["revision"] == item["revision"] and current["status"] == "accepted"
    assert current["publication_status"] == "published"


def test_signed_withdrawal_recovers_after_editorial_commit_interruption(setup):
    app, web, _ = setup
    item = publish_sample(web)
    store = app.state.store
    # Simulate process loss after the immutable signed file was committed but
    # before the app's editorial transaction could update its asset row.
    withdraw_assets(store.root / "published", [item], "admin", "合成故障恢复测试")
    assert store.asset(item["id"])["status"] == "accepted"
    restarted = Store(store.root)
    current = restarted.asset(item["id"])
    assert current["status"] == "excluded" and current["publication_status"] == "withdrawn"
    assert current["audit"][0]["action"] == "withdraw_recovered"
    assert current["audit"][0]["details"]["receipt_status"] == "unconfirmed"
    revision = current["revision"]
    assert Store(store.root).asset(item["id"])["revision"] == revision


def test_backup_restores_signed_revocations_without_private_signing_key(setup, tmp_path):
    app, web, key = setup
    item = publish_sample(web)
    assert web.post(BASE + "/withdraw", json={"asset_ids": [item["id"]], "reason": "备份恢复合成测试"}).status_code == 200
    store = app.state.store
    snapshot = backup(store.root, tmp_path / "snapshot-with-revocation")
    assert (snapshot / "published" / "withdrawals" / "1.json").exists()
    assert not list(snapshot.rglob("*.pem"))
    destination = restore(snapshot, tmp_path / "restored-with-revocation")
    current = Store(destination).asset(item["id"])
    assert current["status"] == "excluded" and current["publication_status"] == "withdrawn"
    document = json.loads((destination / "published" / "withdrawals" / "1.json").read_text())
    verify_signature(document, {"synthetic-key": key.public_key()})


def test_withdrawal_requires_admin_csrf_and_reason(setup):
    app, web, _ = setup
    item = publish_sample(web)
    operator = TestClient(app)
    logged = operator.post(BASE + "/session", json={"username": "operator", "password": "synthetic-password"}).json()
    operator.headers["X-CSRF-Token"] = logged["csrf_token"]
    body = {"asset_ids": [item["id"]], "reason": "合成撤回"}
    assert operator.post(BASE + "/withdraw", json=body).status_code == 403
    assert web.post(BASE + "/withdraw", json=body, headers={"X-CSRF-Token": ""}).status_code == 403
    assert web.post(BASE + "/withdraw", json={**body, "reason": ""}).status_code == 422


def test_restore_rejects_traversal_that_resolves_inside_source_but_outside_target(setup, tmp_path):
    app, web, _ = setup
    item = publish_sample(web)
    snapshot = backup(app.state.store.root, tmp_path / "original" / "snapshot")
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    image_name = next(name for name in manifest["files"] if name.startswith("images/"))
    attack_name = "../snapshot/" + image_name
    manifest["files"][attack_name] = manifest["files"][image_name]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    outside = tmp_path / "other" / "snapshot" / image_name
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"do not overwrite")
    destination = tmp_path / "other" / "restored"
    with pytest.raises(ValueError, match="路径"):
        restore(snapshot, destination)
    assert outside.read_bytes() == b"do not overwrite" and not destination.exists()


def test_analysis_shutdown_does_not_release_lifecycle_before_inflight_finishes(setup):
    app, _, _ = setup
    service = AnalysisService(app.state.store)
    release, entered = threading.Event(), threading.Event()
    def inflight():
        entered.set()
        release.wait(5)
    service._thread = threading.Thread(target=inflight)
    service._thread.start()
    assert entered.wait(1)
    stopping = threading.Thread(target=service.stop)
    stopping.start()
    try:
        time.sleep(2.1)
        assert stopping.is_alive()
    finally:
        release.set()
        stopping.join(2)
    assert not stopping.is_alive()
