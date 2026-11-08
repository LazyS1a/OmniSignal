"""Snapshot integration through the real offline workbench and scoped HTTP feed."""
import hashlib
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from PIL import Image

from omnisignal.knowledge import snapshot_v3 as v3
from omnisignal.knowledge.release import KnowledgeError, read_latest
from omnisignal.workbench.feed import router
from omnisignal.workbench.publishing import (current_publication_cases, current_publication_latest,
    publication_signed_exists, publish_store_assets, withdraw_assets, withdraw_publication_targets)
from test_workbench_library_publication import setup, enable_library, RECIPIENT, POLICY
from test_workbench_auto_editorial import asset, complete
from test_workbench_publication_use import raw


def published(setup, monkeypatch):
    store, analysis, auto, provider, key = setup
    monkeypatch.setenv(v3.ENABLE_ENV, "true")
    target = enable_library(store, auto)
    item, _ = complete(store, analysis, auto, asset(store))
    result = auto.tick()
    assert result["published"] == 1, result
    return store, analysis, auto, provider, key, target, item


def test_workbench_v3_accumulates_catalog_without_legacy_pointer_or_new_rights(setup, monkeypatch):
    store, analysis, auto, provider, key, target, item = published(setup, monkeypatch)
    first = v3.read_snapshot_latest(target)
    assert read_latest(target) is None and publication_signed_exists(target, first["release_id"])
    assert set(current_publication_cases(target)) == {item["id"]}
    second, _ = complete(store, analysis, auto, asset(store, 1))
    result = auto.tick()
    assert result["published"] == 1, result
    assert len(current_publication_cases(target)) == 2 and provider.calls == 2
    manifest = v3.read_manifest(target, current_publication_latest(target)["release_id"], {"synthetic-key": key.public_key()})
    assert manifest["records_count"] == 2 and manifest["recipient"] == RECIPIENT
    assert raw(store, item["id"])["permissions"]["display"] == "unknown"
    assert raw(store, item["id"])["permissions"]["export"] == "unknown"
    assert auto.tick()["published"] == 0


@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_snapshot_round_trip_through_verified_inventory_and_feed(setup, monkeypatch, progressive):
    store, analysis, auto, provider, key = setup
    monkeypatch.setenv(v3.ENABLE_ENV, "true")
    target = enable_library(store, auto)
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "green").save(output, "JPEG", progressive=progressive)
    image_bytes = output.getvalue()
    item = store.import_asset({"title": "Synthetic JPEG", "source_id": "potw",
        "source_url": "https://example.invalid/synthetic/jpeg", "description": "Synthetic image only."}, image_bytes)
    item, _ = complete(store, analysis, auto, item)
    result = auto.tick()
    assert result["published"] == 1 and provider.calls == 1
    pointer = v3.read_snapshot_latest(target)
    manifest = v3.read_manifest(target, pointer["release_id"], {"synthetic-key": key.public_key()})
    records, images = v3.snapshot_inventory(target, manifest, verify_images=True)
    assert len(records) == 1 and manifest["image_count"] == 1
    assert {image["mime"] for image in images.values()} == {"image/jpeg"}
    token = "synthetic-jpeg-feed-token-0000000000"
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_READ_TOKEN_SHA256", hashlib.sha256(token.encode()).hexdigest())
    app = FastAPI()
    prefix = "/knowledge/v2/libraries/{library_id}"
    app.include_router(router(store.root / "published", prefix=prefix, policy_loader=auto.configuration))
    with TestClient(app) as client:
        response = client.get("/knowledge/v2/libraries/" + RECIPIENT["library_id"] + "/snapshots/releases/"
            + pointer["release_id"] + "/objects/" + hashlib.sha256(image_bytes).hexdigest(),
            headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200 and response.content == image_bytes
    assert auto.tick()["published"] == 0 and provider.calls == 1


def test_v3_only_case_can_be_withdrawn_and_disabling_switch_keeps_history(setup, monkeypatch):
    store, _, _, _, _, target, item = published(setup, monkeypatch)
    before = current_publication_latest(target)
    monkeypatch.setenv(v3.ENABLE_ENV, "false")
    assert current_publication_latest(target) == before and item["id"] in current_publication_cases(target)
    result = withdraw_publication_targets(store.root/"published", [raw(store, item["id"])], "synthetic-admin", "Synthetic withdrawal")
    assert result["status"] == "withdrawn" and result["assets_count"] == 1
    assert item["id"] not in current_publication_cases(target)


def test_v3_withdrawal_cannot_be_signed_for_another_recipient(setup, monkeypatch):
    store, _, _, _, _, target, item = published(setup, monkeypatch)
    wrong = {**RECIPIENT, "library_id": "another-library"}
    with pytest.raises(KnowledgeError, match="withdrawal recipient"):
        withdraw_assets(target, [raw(store, item["id"])], "synthetic-admin", "Synthetic withdrawal", recipient=wrong)
    assert not (target/"withdrawals").exists()


def test_snapshot_feed_enforces_token_scope_membership_hash_and_switch(setup, monkeypatch):
    store, _, auto, _, _, target, item = published(setup, monkeypatch)
    token = "synthetic-feed-identity-01234567890123456789"
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_READ_TOKEN_SHA256", hashlib.sha256(token.encode()).hexdigest())
    app = FastAPI()
    app.include_router(router(store.root/"published", prefix="/knowledge/v2/libraries/{library_id}", policy_loader=auto.configuration))
    client = TestClient(app); headers = {"Authorization": "Bearer "+token}
    url = "/knowledge/v2/libraries/"+RECIPIENT["library_id"]
    assert client.get(url+"/snapshots/latest").status_code == 401
    pointer = client.get(url+"/snapshots/latest", headers=headers).json()["release"]
    release = url+"/snapshots/releases/"+pointer["release_id"]
    manifest = client.get(release+"/manifest", headers=headers).json()
    assert manifest["schema_version"] == 3 and manifest["recipient"] == RECIPIENT
    part = manifest["parts"][0]
    response = client.get(release+"/parts/"+part["sha256"], headers=headers)
    assert response.status_code == 200
    image = response.json()["objects"][0]
    assert client.get(release+"/objects/"+image["sha256"], headers=headers).status_code == 200
    assert client.get(release+"/objects/"+"f"*64, headers=headers).status_code == 404
    assert client.get(release.replace(RECIPIENT["library_id"], "wrong-library")+"/manifest", headers=headers).status_code == 404
    assert client.get(url+"/latest", headers=headers).json()["status"] == "empty"
    (v3.storage(target)/"objects"/image["sha256"]).write_bytes(b"tampered")
    assert client.get(release+"/objects/"+image["sha256"], headers=headers).status_code == 404
    monkeypatch.setenv(v3.ENABLE_ENV, "false")
    assert client.get(url+"/snapshots/latest", headers=headers).status_code == 404


def test_workbench_snapshot_recovery_replays_frozen_membership(setup, monkeypatch):
    store, _, _, _, _, target, item = published(setup, monkeypatch)
    stored = raw(store, item["id"])
    pointer = current_publication_latest(target)
    result = publish_store_assets(store, [item["id"]], "system:auto-editorial", automated_only=True,
                                  publication_policy=POLICY)
    assert result["replayed"] is True and current_publication_latest(target) == pointer


@pytest.mark.parametrize("failure", [
    "snapshot record count limit", "snapshot part count limit", "snapshot partition depth exhausted",
    "snapshot manifest size limit", "snapshot image capacity exceeded",
    "snapshot retained capacity exceeded", "snapshot disk capacity unavailable",
])
def test_auto_snapshot_capacity_errors_have_explicit_blocked_status(setup, monkeypatch, failure):
    import omnisignal.workbench.auto_editorial as worker
    store, analysis, auto, provider, _ = setup
    monkeypatch.setenv(v3.ENABLE_ENV, "true")
    target = enable_library(store, auto)
    complete(store, analysis, auto, asset(store))
    def fail(*args, **kwargs):
        raise KnowledgeError(failure)
    monkeypatch.setattr(worker, "publish_store_assets", fail)
    result = auto.tick()
    assert result["published"] == 0
    assert result["blocked_reasons"]["publication_capacity_exceeded"] == 1
    assert result["publication_capacity"] == {"status": "blocked", "reason": "final_release_capacity_exceeded"}
    assert v3.read_snapshot_latest(target) is None and provider.calls == 1


@pytest.mark.parametrize("failure", [
    "snapshot recipient mismatch", "snapshot content hash mismatch", "invalid signature",
    "snapshot image changed after capacity preflight",
])
def test_auto_snapshot_permissions_and_tampering_do_not_masquerade_as_capacity(setup, monkeypatch, failure):
    import omnisignal.workbench.auto_editorial as worker
    store, analysis, auto, provider, _ = setup
    monkeypatch.setenv(v3.ENABLE_ENV, "true")
    enable_library(store, auto)
    complete(store, analysis, auto, asset(store))
    def fail(*args, **kwargs):
        raise KnowledgeError(failure)
    monkeypatch.setattr(worker, "publish_store_assets", fail)
    result = auto.tick()
    assert result["published"] == 0 and result["blocked_reasons"]["publication_blocked"] == 1
    assert "publication_capacity_exceeded" not in result["blocked_reasons"]
    assert result["publication_capacity"]["status"] != "blocked" and provider.calls == 1


def test_real_retained_budget_blocks_auto_publication_and_keeps_frozen_batch(setup, monkeypatch):
    store, analysis, auto, provider, _ = setup
    monkeypatch.setenv(v3.ENABLE_ENV, "true")
    target = enable_library(store, auto)
    complete(store, analysis, auto, asset(store))
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", "1")
    signatures = []
    monkeypatch.setattr(v3, "_signed", lambda *args: signatures.append(args))
    first = auto.tick()
    assert first["published"] == 0 and first["blocked_reasons"]["publication_capacity_exceeded"] == 1
    assert first["publication_capacity"] == {"status": "blocked", "reason": "final_release_capacity_exceeded"}
    key = auto._publication_batch_key(auto.configuration())
    with store.db() as db:
        frozen = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]
    second = auto.tick()
    assert second["published"] == 0 and second["blocked_reasons"]["publication_capacity_exceeded"] == 1
    with store.db() as db:
        assert db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0] == frozen
    assert provider.calls == 1 and signatures == [] and v3.read_snapshot_latest(target) is None
    assert v3.retained_content_bytes(target) == 0
