"""Internal-use policy acceptance with isolated synthetic files and real signatures."""
from copy import deepcopy
import json

import pytest

from omnisignal.knowledge.release import materialize_release, read_latest, verify_release
from omnisignal.workbench.auto_editorial import validate_config
from omnisignal.workbench.publication_use import validate_policy
from omnisignal.workbench.publishing import publish_store_assets, withdraw_assets
from omnisignal.workbench.review import fresh_review
from omnisignal.workbench.store import Conflict, encode
from test_workbench_auto_editorial import ACTOR, CONFIG, asset, complete, setup

POLICY = {"policy_id": "synthetic-internal-use-v1", "authorized_at": "2026-11-02T12:00:00+08:00",
          "purpose": "company_internal_inspiration", "source_ids": ["potw", "adsofbrands", "bpando", "itsnicethat"],
          "recipient": {"origin": "https://preview.example.invalid", "workspace_id": "synthetic-workspace"}}


def enable(auto):
    auto._config = {**deepcopy(CONFIG), "publication_authorized": deepcopy(POLICY)}


def raw(store, asset_id):
    with store.db() as db:
        return json.loads(db.execute("SELECT document FROM assets WHERE id=?", (asset_id,)).fetchone()[0])


def grants(store):
    with store.db() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT document FROM publication_use_grants")]


def test_policy_publishes_current_ai_inventory_without_changing_content_permissions_or_jobs(setup):
    store, analysis, auto, provider, key = setup
    item, _ = complete(store, analysis, auto, asset(store))
    assert auto.tick()["reviewed"] == 1
    before = raw(store, item["id"])
    with store.db() as db:
        jobs_before = [tuple(row) for row in db.execute("SELECT * FROM analysis_jobs")]
    enable(auto)
    tick = auto.tick()
    assert tick["reviewed"] == 0 and tick["published"] == 1 and provider.calls == 1
    after = raw(store, item["id"])
    assert {k: v for k, v in after.items() if k not in {"publication_status", "publication_version"}} == before
    assert after["permissions"] == {"analysis": "allowed", "display": "unknown", "export": "unknown"}
    assert fresh_review(after)["provenance"] == "model_automated_review"
    assert store.asset(item["id"])["publication_use_status"] == "internal_policy"
    with store.db() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM analysis_jobs")] == jobs_before
        assert db.execute("SELECT count(*) FROM audit WHERE action='internal_publication_authorized'").fetchone()[0] == 1
    _, records = verify_release(store.root / "published" / "releases" / tick["releases"][0]["version"],
                                {"synthetic-key": key.public_key()})
    case = records[0]["case"]
    basis = case["editorial"]["publication_use_basis"]
    assert basis == grants(store)[0]["basis"]
    assert basis["recipient"] == POLICY["recipient"] and basis["authorization_source"] == "operator_policy"
    assert basis["asset_id"] == case["case_id"] and basis["sha256"] == after["sha256"]
    assert case["permissions"]["export"] == case["permissions"]["display"] == "allowed"
    assert auto.status()["publication_authorized"] is True and auto.status()["today"]["permission_blocked"] == 0
    assert auto.tick()["published"] == 0 and len(grants(store)) == 1 and provider.calls == 1


@pytest.mark.parametrize("field,value", [("display", "denied"), ("export", "denied"), ("analysis", "denied")])
def test_explicit_denial_is_never_overridden(setup, field, value):
    store, analysis, auto, provider, _ = setup
    item = asset(store, analysis="allowed")
    store.review(item["id"], {"expected_revision": item["revision"],
        "permissions": {**item["permissions"], field: value}, "permission_note": "Synthetic explicit denial."}, "admin", "admin")
    enable(auto)
    if field != "analysis":
        complete(store, analysis, auto, item)
    tick = auto.tick()
    assert tick["published"] == 0 and grants(store) == []
    assert raw(store, item["id"])["permissions"][field] == "denied"


def test_policy_does_not_authorize_source_outside_its_subset(setup):
    store, analysis, auto, _, _ = setup
    complete(store, analysis, auto, asset(store))
    enable(auto)
    auto._config["publication_authorized"]["source_ids"] = ["bpando"]
    assert auto.tick()["blocked_reasons"]["display_export_permission_required"] == 1
    assert grants(store) == []


@pytest.mark.parametrize("mutation", ["purpose", "time", "source", "origin", "workspace", "extra", "null_recipient"])
def test_publication_policy_is_strict_and_scoped(mutation):
    policy = deepcopy(POLICY)
    if mutation == "purpose": policy["purpose"] = "public_reuse"
    if mutation == "time": policy["authorized_at"] = "2026-11-02"
    if mutation == "source": policy["source_ids"] = ["manual"]
    if mutation == "origin": policy["recipient"]["origin"] += "/path"
    if mutation == "workspace": policy["recipient"]["workspace_id"] = "../../other"
    if mutation == "extra": policy["copyright_owned"] = True
    if mutation == "null_recipient": policy["recipient"] = None
    with pytest.raises(Conflict): validate_policy(policy)
    assert validate_config(CONFIG) == CONFIG


def test_policy_must_fit_editorial_source_scope():
    with pytest.raises(Conflict, match="publication_source_scope"):
        validate_config({**CONFIG, "source_ids": ["potw"], "publication_authorized": POLICY})


@pytest.mark.parametrize("change", ["disable", "replace"])
def test_policy_is_rechecked_at_signature_and_failed_check_rolls_back_grants(setup, change):
    store, analysis, auto, provider, _ = setup
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    current = raw(store, item["id"])
    enable(auto)
    count = 0
    def changed_policy():
        nonlocal count
        count += 1
        config = deepcopy(auto._config)
        if count == 2:
            if change == "disable": config["auto_publish"] = False
            else: config["publication_authorized"]["policy_id"] = "replacement"
        return config
    with pytest.raises(Conflict, match="publication_policy_changed"):
        publish_store_assets(store, [item["id"]], ACTOR, automated_only=True,
            publication_policy=POLICY, policy_loader=changed_policy,
            expected_revisions={item["id"]: current["revision"]})
    assert grants(store) == [] and not (store.root / "published" / "latest.json").exists()
    assert raw(store, item["id"]) == current and provider.calls == 1


def test_signed_crash_reconstructs_same_deterministic_grant_and_replays_once(setup, monkeypatch):
    store, analysis, auto, provider, _ = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    audit = store.audit
    def crash(db, entity, actor, action, details=None):
        if action == "publish": raise RuntimeError("synthetic crash after signature")
        return audit(db, entity, actor, action, details)
    monkeypatch.setattr(store, "audit", crash)
    with pytest.raises(RuntimeError): auto.tick()
    release_id = read_latest(store.root / "published")["release_id"]
    assert grants(store) == [] and raw(store, item["id"]).get("publication_status") != "published"
    monkeypatch.setattr(store, "audit", audit)
    tick = auto.tick()
    assert tick["published"] == 1 and tick["releases"][0]["version"] == release_id
    assert len(grants(store)) == 1 and len(list((store.root / "published" / "releases").iterdir())) == 1
    assert auto.tick()["published"] == 0 and provider.calls == 1


def test_checkpoints_keep_signed_scoped_grants_and_legacy_allowed_inventory(setup, monkeypatch):
    import omnisignal.workbench.publishing as publishing
    store, analysis, auto, _, key = setup
    monkeypatch.setattr(publishing, "CHECKPOINT_CHAIN_LIMIT", 2)
    first, _ = complete(store, analysis, auto, asset(store, publish=True))
    assert auto.tick()["published"] == 1
    enable(auto)
    for index in (1, 2):
        complete(store, analysis, auto, asset(store, index))
        assert auto.tick()["published"] == 1
    latest = read_latest(store.root / "published")
    manifest, records = verify_release(store.root / "published" / "releases" / latest["release_id"],
                                      {"synthetic-key": key.public_key()})
    assert manifest["kind"] == "full" and len(records) == 3 and len(grants(store)) == 2
    cases = materialize_release(store.root / "published")
    assert "publication_use_basis" not in cases[first["id"]]["editorial"]
    assert sum("publication_use_basis" in case["editorial"] for case in cases.values()) == 2


def test_changed_source_invalidates_grant_and_blocks_carry_forward(setup):
    store, analysis, auto, _, _ = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    assert auto.tick()["published"] == 1
    with store.db() as db:
        changed = json.loads(db.execute("SELECT document FROM assets WHERE id=?", (item["id"],)).fetchone()[0])
        changed["description"] += " source input changed without a revision, synthetic corruption"
        db.execute("UPDATE assets SET document=? WHERE id=?", (encode(changed), item["id"]))
    assert store.asset(item["id"])["publication_use_status"] == "stale"
    complete(store, analysis, auto, asset(store, 1))
    assert auto.tick()["published"] == 0
    assert len(list((store.root / "published" / "releases").iterdir())) == 1


def test_stale_review_cannot_create_internal_use_grant(setup):
    store, analysis, auto, provider, _ = setup
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    current = store.asset(item["id"])
    store.review(item["id"], {"expected_revision": current["revision"], "description": "人工修改过的源说明"}, "admin", "admin")
    enable(auto)
    assert auto.tick()["published"] == 0 and grants(store) == [] and provider.calls == 1


def test_v2_quality_conflict_is_not_overridden_by_operator_policy(setup):
    from test_workbench_design_review import analyzed
    store, _, auto, _, item, _, calls = analyzed(setup, publish=False, conflict=True)
    enable(auto)
    tick = auto.tick()
    assert tick["reviewed"] == tick["published"] == 0 and grants(store) == [] and len(calls) == 1


def test_explicit_manual_publish_cannot_create_operator_policy_grant(setup):
    store, analysis, auto, _, _ = setup
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    with pytest.raises(Conflict):
        publish_store_assets(store, [item["id"]], "admin", publication_policy=POLICY)
    with pytest.raises(Conflict): publish_store_assets(store, [item["id"]], "admin")
    assert grants(store) == []


def test_new_rule_quality_pass_can_publish_without_reanalysis(setup):
    from test_workbench_design_review import analyzed
    store, _, auto, _, item, _, calls = analyzed(setup, publish=False)
    assert auto.tick()["reviewed"] == 1
    before = raw(store, item["id"])
    enable(auto)
    assert auto.tick()["published"] == 1
    after = raw(store, item["id"])
    assert after["analysis_quality_review"] == before["analysis_quality_review"]
    assert store.asset(item["id"])["publication_use_status"] == "internal_policy"
    assert len(calls) == 1


def test_withdrawn_internal_case_cannot_be_republished_or_carried_forward(setup):
    store, analysis, auto, _, _ = setup
    enable(auto)
    item, _ = complete(store, analysis, auto, asset(store))
    assert auto.tick()["published"] == 1
    withdraw_assets(store.root / "published", [raw(store, item["id"])], "admin", "Synthetic withdrawal")
    store.recover_withdrawals()
    assert auto.tick()["published"] == 0
    assert raw(store, item["id"])["status"] == "excluded"
    assert raw(store, item["id"])["publication_status"] == "withdrawn"
    complete(store, analysis, auto, asset(store, 1))
    assert auto.tick()["published"] == 1
    assert item["id"] not in materialize_release(store.root / "published")


def test_failed_paid_job_is_not_retried_or_granted_by_policy(setup):
    from omnisignal.workbench.analysis import AnalysisFailure
    store, analysis, auto, provider, _ = setup
    provider.output = AnalysisFailure("synthetic failure")
    enable(auto)
    complete(store, analysis, auto, asset(store))
    assert auto.tick()["published"] == auto.tick()["reviewed"] == 0
    assert grants(store) == [] and provider.calls == 1


def test_policy_alone_does_not_enable_disabled_publication(setup):
    store, analysis, auto, provider, _ = setup
    complete(store, analysis, auto, asset(store))
    enable(auto)
    auto._config["auto_publish"] = False
    assert auto.tick()["reviewed"] == 1
    assert auto.tick()["published"] == 0 and grants(store) == [] and provider.calls == 1


def test_corrupt_index_revision_does_not_receive_policy_or_starve_valid_candidate(setup):
    store, analysis, auto, _, _ = setup
    item, _ = complete(store, analysis, auto, asset(store))
    auto.tick()
    with store.db() as db:
        db.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (item["id"],))
    other, _ = complete(store, analysis, auto, asset(store, 1))
    enable(auto)
    tick = auto.tick()
    assert tick["published"] == 1 and tick["blocked_reasons"]["storage_inconsistent"] == 1
    assert set(materialize_release(store.root / "published")) == {other["id"]}
    assert all(grant["basis"]["asset_id"] != item["id"] for grant in grants(store))
    with pytest.raises(Conflict, match="存储版本不一致"):
        publish_store_assets(store, [item["id"]], ACTOR, automated_only=True, publication_policy=POLICY)


def test_revoking_policy_during_image_preparation_prevents_signature_and_rolls_back_grant(setup, monkeypatch):
    import omnisignal.workbench.publishing as publishing
    store, analysis, auto, provider, _ = setup
    complete(store, analysis, auto, asset(store))
    enable(auto)
    prepare = publishing._prepare_asset
    def revoke_after_preparation(item, stage):
        result = prepare(item, stage)
        auto._config["auto_publish"] = False
        return result
    monkeypatch.setattr(publishing, "_prepare_asset", revoke_after_preparation)
    tick = auto.tick()
    assert tick["published"] == 0 and tick["blocked_reasons"]["publication_blocked"] == 1
    assert grants(store) == [] and provider.calls == 1
    assert not (store.root / "published" / "latest.json").exists()
    assert not list((store.root / "published" / "releases").glob("*"))


@pytest.mark.parametrize("config", [None, {"invalid": "configuration"}])
def test_missing_or_invalid_config_keeps_status_readable_with_accepted_ai_inventory(setup, config):
    store, analysis, auto, _, _ = setup
    complete(store, analysis, auto, asset(store))
    auto.tick()
    auto._config = config
    snapshot = auto.status()
    assert snapshot["status"] == ("disabled" if config is None else "invalid_config")
    assert snapshot["publication_authorized"] is False and snapshot["today"]["permission_blocked"] == 0


@pytest.mark.parametrize("field,value", [
    ("policy_id", "-bad"), ("policy_id", "_bad"),
    ("authorized_at", "2026-11-02 12:00:00+08:00"),
    ("authorized_at", "2026-11-02T12:00:00+0800"),
    ("origin", "https://Preview.example.invalid"),
    ("origin", "https://preview.example.invalid:443"),
    ("origin", "https://preview.example.invalid:0444"),
    ("origin", "https://127.1"),
])
def test_policy_cannot_sign_values_rejected_by_receiver_canonical_contract(field, value):
    policy = deepcopy(POLICY)
    if field == "origin": policy["recipient"][field] = value
    else: policy[field] = value
    with pytest.raises(Conflict): validate_policy(policy)
