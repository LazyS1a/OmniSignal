"""Shared-library scope migration with synthetic images and offline model fixtures."""
from copy import deepcopy
import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omnisignal.knowledge.release import (KnowledgeError, delete_record, make_record, materialize_release,
    publish_release, publish_withdrawals, read_latest, verify_release, verify_signature)
from omnisignal.knowledge.scope import recipient_key, validate_library_recipient
from omnisignal.workbench.feed import router
from omnisignal.workbench.library_publication import (initialize_library_feed, library_rebaseline_plan,
    library_root, target_root)
from omnisignal.workbench.publication_use import describe_current_target, existing_basis, validate_policy
from omnisignal.workbench.publishing import publish_store_assets, withdraw_publication_targets
from omnisignal.workbench.store import Conflict, encode
from test_workbench_auto_editorial import ACTOR, CONFIG, asset, complete, setup as base_setup
from test_workbench_publication_use import POLICY as OLD_POLICY, enable, grants, raw

RECIPIENT = {"kind": "library", "origin": "https://preview.example.invalid", "library_id": "synthetic-company-library"}
POLICY = {**deepcopy(OLD_POLICY), "policy_id": "synthetic-library-use-v2", "recipient": RECIPIENT}


@pytest.fixture
def setup(tmp_path_factory, monkeypatch):
    # Two immutable namespaces + hash-addressed image names otherwise exceed
    # Windows MAX_PATH when pytest includes each long test name in tmp_path.
    return base_setup.__wrapped__(tmp_path_factory.mktemp("lib"), monkeypatch)


def enable_library(store, auto):
    plan = library_rebaseline_plan(store, POLICY)
    initialize_library_feed(store, POLICY, plan["plan_sha256"])
    auto._config = {**deepcopy(CONFIG), "publication_authorized": deepcopy(POLICY)}
    return target_root(store, POLICY)


def table(store, name):
    with store.db() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM " + name)]


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and "libraries" not in path.relative_to(root).parts}


def test_explicit_new_feed_rebaseline_preserves_v1_grants_source_and_jobs(setup):
    store, analysis, auto, provider, key = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    assert auto.tick()["published"] == 1
    old_files, old_grants, before = files(store.root / "published"), grants(store), raw(store, item["id"])
    old_jobs = table(store, "analysis_jobs")
    plan = library_rebaseline_plan(store, POLICY)
    assert plan["eligible_count"] == 1 and plan["model_calls"] == 0
    target = enable_library(store, auto)
    assert auto.tick()["published"] == 1
    assert auto.tick()["published"] == 0
    assert files(store.root / "published") == old_files and grants(store) == old_grants
    assert raw(store, item["id"]) == before and table(store, "analysis_jobs") == old_jobs and provider.calls == 1
    latest = read_latest(target)
    manifest, records = verify_release(target / "releases" / latest["release_id"], {"synthetic-key": key.public_key()})
    assert manifest["schema_version"] == 2 and manifest["recipient"] == RECIPIENT and manifest["kind"] == "full"
    assert manifest["sequence"] == 1 and manifest["parent_release_id"] is None
    basis = records[0]["case"]["editorial"]["publication_use_basis"]
    assert basis["schema_version"] == 2 and basis["recipient"] == RECIPIENT
    assert basis["revision"] == before["revision"]
    with store.db() as db:
        assert existing_basis(db, before)["schema_version"] == 1
        assert existing_basis(db, before, recipient=RECIPIENT) == basis
    assert len(table(store, "publication_library_grants")) == len(table(store, "publication_target_deliveries")) == 1


def test_previously_allowed_case_still_needs_new_library_grant(setup):
    store, analysis, auto, provider, key = setup
    item, _ = complete(store, analysis, auto, asset(store, publish=True))
    assert auto.tick()["published"] == 1
    assert grants(store) == []
    target = enable_library(store, auto)
    assert auto.tick()["published"] == 1
    signed = materialize_release(target)[item["id"]]
    assert signed["editorial"]["publication_use_basis"]["schema_version"] == 2
    assert len(table(store, "publication_library_grants")) == 1 and grants(store) == [] and provider.calls == 1


def test_library_activation_requires_explicit_current_rebaseline_plan(setup):
    store, analysis, auto, provider, _ = setup
    complete(store, analysis, auto, asset(store))
    auto.tick()
    auto._config = {**deepcopy(CONFIG), "publication_authorized": deepcopy(POLICY)}
    assert auto.tick()["blocked_reasons"]["publication_blocked"] == 1
    assert not (target_root(store, POLICY) / "latest.json").exists()
    with pytest.raises(Conflict, match="plan_changed"):
        initialize_library_feed(store, POLICY, "0" * 64)
    assert table(store, "publication_library_grants") == [] and provider.calls == 1


@pytest.mark.parametrize("change", ["workspace", "kind", "extra", "origin", "library", "empty", "schema"])
def test_library_recipient_never_degrades_to_workspace_or_wildcard(change):
    value = deepcopy(RECIPIENT)
    if change == "workspace": value["workspace_id"] = "old-private"
    if change == "kind": value["kind"] = "workspace"
    if change == "extra": value["any_tenant"] = True
    if change == "origin": value["origin"] += "/"
    if change == "library": value["library_id"] = "*"
    if change == "empty": value["library_id"] = ""
    if change == "schema": value.pop("kind")
    with pytest.raises(ValueError): validate_library_recipient(value)
    with pytest.raises(ValueError): validate_policy({**POLICY, "recipient": value})
    assert validate_policy(OLD_POLICY) == OLD_POLICY


def test_rebaseline_does_not_adopt_old_pending_batch_or_expand_source_permissions(setup):
    store, analysis, auto, provider, _ = setup
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    outside = asset(store, 1, source="manual")
    old_batch = [{"id": "old-unresolved", "revision": 1, "sha256": "0" * 64}]
    with store.db() as db:
        db.execute("INSERT INTO meta VALUES('auto_editorial_publication_batch',?)", (encode(old_batch),))
    target = enable_library(store, auto)
    assert auto.tick()["published"] == 1 and list(materialize_release(target)) == [item["id"]]
    with store.db() as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='auto_editorial_publication_batch'").fetchone()[0]) == old_batch
    assert raw(store, outside["id"])["permissions"]["analysis"] == "unknown" and provider.calls == 1


def test_library_full_checkpoint_only_carries_its_own_signed_basis(setup, monkeypatch):
    import omnisignal.workbench.publishing as publishing
    store, analysis, auto, _, key = setup
    enable(auto)
    complete(store, analysis, auto, asset(store))
    auto.tick()
    target = enable_library(store, auto)
    monkeypatch.setattr(publishing, "CHECKPOINT_CHAIN_LIMIT", 2)
    auto.tick()
    for index in (1, 2):
        complete(store, analysis, auto, asset(store, index))
        assert auto.tick()["published"] == 1
    latest = read_latest(target)
    manifest, records = verify_release(target / "releases" / latest["release_id"], {"synthetic-key": key.public_key()})
    assert manifest["kind"] == "full" and len(records) == 3
    assert all(record["case"]["editorial"]["publication_use_basis"]["recipient"] == RECIPIENT for record in records)
    assert len(materialize_release(store.root / "published")) == 1


def test_library_signature_crash_replays_without_new_analysis_or_source_mutation(setup, monkeypatch):
    store, analysis, auto, provider, _ = setup
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    before = raw(store, item["id"])
    target = enable_library(store, auto)
    original = store.audit
    def crash(db, entity, actor, action, details=None):
        if action == "publish": raise RuntimeError("synthetic-post-signature-failure")
        return original(db, entity, actor, action, details)
    monkeypatch.setattr(store, "audit", crash)
    with pytest.raises(RuntimeError): auto.tick()
    release = read_latest(target)
    assert table(store, "publication_library_grants") == table(store, "publication_target_deliveries") == []
    monkeypatch.setattr(store, "audit", original)
    assert auto.tick()["published"] == 1 and read_latest(target) == release
    assert raw(store, item["id"]) == before and provider.calls == 1


def test_policy_revoked_while_preparing_library_package_never_signs(setup, monkeypatch):
    import omnisignal.workbench.publishing as publishing
    store, analysis, auto, provider, _ = setup
    complete(store, analysis, auto, asset(store))
    auto.tick()
    target = enable_library(store, auto)
    original = publishing._prepare_asset
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        auto._config["auto_publish"] = False
        return result
    monkeypatch.setattr(publishing, "_prepare_asset", revoke)
    assert auto.tick()["published"] == 0
    assert not (target / "latest.json").exists() and table(store, "publication_library_grants") == [] and provider.calls == 1


def test_global_withdrawal_signs_both_old_and_shared_feeds_and_recovery_blocks_reentry(setup):
    store, analysis, auto, provider, key = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    target = enable_library(store, auto)
    auto.tick()
    result = withdraw_publication_targets(store.root / "published", [raw(store, item["id"])], "admin", "Synthetic withdrawal")
    assert len(result["targets"]) == 2
    old = json.loads((store.root / "published/withdrawals/1.json").read_text())
    shared = json.loads((target / "withdrawals/1.json").read_text())
    verify_signature(old, {"synthetic-key": key.public_key()}); verify_signature(shared, {"synthetic-key": key.public_key()})
    assert old["schema_version"] == 1 and "recipient" not in old
    assert shared["schema_version"] == 2 and shared["recipient"] == RECIPIENT
    store.recover_withdrawals()
    assert raw(store, item["id"])["publication_status"] == "withdrawn"
    assert auto.tick()["published"] == 0 and not materialize_release(target) and provider.calls == 1


def test_shared_feed_auth_scope_and_empty_discovery_keep_old_feed_available(setup, monkeypatch):
    store, analysis, auto, _, key = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    old_release = read_latest(store.root / "published")["release_id"]
    target = enable_library(store, auto)
    token = "synthetic-offline-feed-identity-0123456789"
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_READ_TOKEN_SHA256", hashlib.sha256(token.encode()).hexdigest())
    app = FastAPI()
    app.include_router(router(store.root / "published"))
    app.include_router(router(store.root / "published", prefix="/knowledge/v2/libraries/{library_id}", policy_loader=auto.configuration))
    client = TestClient(app); headers = {"Authorization": "Bearer " + token}
    url = "/knowledge/v2/libraries/" + RECIPIENT["library_id"]
    assert client.get(url + "/latest").status_code == 401
    empty = client.get(url + "/latest", headers=headers).json()
    assert empty == {"status": "empty", "release": None, "schema_version": 2, "recipient": RECIPIENT}
    assert client.get(url.replace(RECIPIENT["library_id"], "other-tenant") + "/latest", headers=headers).status_code == 404
    assert client.get("/knowledge/v1/releases/" + old_release + "/manifest", headers=headers).json()["schema_version"] == 1
    auto.tick()
    pointer = client.get(url + "/latest", headers=headers).json()["release"]
    manifest = client.get(url + "/releases/" + pointer["release_id"] + "/manifest", headers=headers).json()
    assert manifest["schema_version"] == 2 and manifest["recipient"] == RECIPIENT
    assert client.get(url + "/releases/" + old_release + "/manifest", headers=headers).status_code == 404
    assert client.get(url + "/withdrawals/latest", headers=headers).json()["recipient"] == RECIPIENT


def test_v2_manifest_empty_delete_and_withdrawal_are_signed_to_exact_library(setup):
    store, analysis, auto, _, key = setup
    target = enable_library(store, auto)
    args = dict(root=target, asset_root=store.images, private_key=key, key_id="synthetic-key", recipient=RECIPIENT)
    empty = publish_release(**args, release_id="empty-v2", sequence=1, records=[])
    assert empty["schema_version"] == 2 and empty["recipient"] == RECIPIENT
    deleted = publish_release(**args, release_id="delete-v2", sequence=2, records=[delete_record("synthetic", 1)], kind="delta", parent_release_id="empty-v2")
    assert verify_release(target / "releases/delete-v2", {"synthetic-key": key.public_key()})[0] == deleted
    wrong = {**RECIPIENT, "library_id": "other-tenant"}
    with pytest.raises(KnowledgeError, match="recipient"):
        publish_release(**{**args, "recipient": wrong}, release_id="wrong", sequence=3, records=[])
    with pytest.raises(KnowledgeError, match="recipient"):
        publish_withdrawals(root=target, sequence=1, case_ids=["synthetic"], private_key=key, key_id="synthetic-key", recipient=wrong)
    with pytest.raises(KnowledgeError, match="recipient"):
        publish_withdrawals(root=target, sequence=1, case_ids=["synthetic"], private_key=key, key_id="synthetic-key")


def test_shared_scope_cannot_reinterpret_legacy_case_without_new_basis(setup):
    store, analysis, auto, _, key = setup
    enable(auto)
    complete(store, analysis, auto, asset(store))
    auto.tick()
    case = next(iter(materialize_release(store.root / "published").values()))
    target = enable_library(store, auto)
    with pytest.raises(KnowledgeError, match="recipient"):
        publish_release(root=target, release_id="bad", sequence=1, records=[make_record(case)],
                        asset_root=store.images, private_key=key, key_id="synthetic-key", recipient=RECIPIENT)
    assert not (target / "latest.json").exists()


def test_ui_target_projection_never_confuses_private_and_shared_delivery(setup, monkeypatch):
    store, analysis, auto, _, _ = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    before = raw(store, item["id"])
    with store.db() as db:
        assert describe_current_target(db, before, policy=POLICY)["status"] == "not_published"
    enable_library(store, auto)
    config_file = store.root / "synthetic-policy.json"
    config_file.write_text(json.dumps(auto._config), encoding="utf-8")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_AUTO_EDITORIAL_CONFIG", str(config_file))
    assert store.asset(item["id"])["publication_target"]["status"] == "not_published"
    auto.tick()
    derived = store.asset(item["id"])["publication_target"]
    assert derived["status"] == "published" and derived["version"] and derived["receipt_status"] == "unconfirmed"
    assert raw(store, item["id"]) == before
    assert auto.status()["target_delivered_revisions"] == 1
    assert auto.status()["target_latest_release"]["release_id"] == derived["version"]
    with store.db() as db:
        changed = {**before, "description": "Synthetic source changed"}
        db.execute("UPDATE assets SET document=? WHERE id=?", (encode(changed), item["id"]))
    assert store.asset(item["id"])["publication_target"]["status"] == "stale"


def test_partial_cross_target_withdrawal_errors_and_blocks_further_publication(setup, monkeypatch):
    import omnisignal.workbench.publishing as publishing
    store, analysis, auto, provider, _ = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick(); target = enable_library(store, auto); auto.tick()
    original = publishing.withdraw_assets
    def fail_shared(root, assets, actor, reason, *, recipient=None):
        if recipient is not None:
            raise OSError("synthetic filesystem interruption")
        return original(root, assets, actor, reason)
    monkeypatch.setattr(publishing, "withdraw_assets", fail_shared)
    with pytest.raises(OSError):
        withdraw_publication_targets(store.root / "published", [raw(store, item["id"])], "admin", "Synthetic withdrawal")
    assert not materialize_release(store.root / "published") and item["id"] in materialize_release(target)
    with pytest.raises(Conflict, match="永久撤回"):
        publish_store_assets(store, [item["id"]], ACTOR, automated_only=True, publication_policy=POLICY)
    monkeypatch.setattr(publishing, "withdraw_assets", original)
    withdraw_publication_targets(store.root / "published", [raw(store, item["id"])], "admin", "Synthetic withdrawal")
    store.recover_withdrawals()
    assert not materialize_release(target) and provider.calls == 1


def test_shared_only_asset_cannot_bypass_signed_withdrawal_by_changing_permissions(setup):
    store, analysis, auto, _, _ = setup
    item, _ = complete(store, analysis, auto, asset(store)); auto.tick()
    enable_library(store, auto); auto.tick()
    current = raw(store, item["id"])
    assert current.get("publication_status") != "published"
    with pytest.raises(Conflict, match="永久撤回"):
        store.review(item["id"], {"expected_revision": current["revision"], "status": "excluded"}, "admin", "admin")
