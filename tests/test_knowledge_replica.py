"""Synthetic offline protocol tests, not real Nova/provider acceptance data."""
import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from omnisignal.knowledge.release import (
    KnowledgeError, _lock, _signed, canonical, delete_record, digest,
    make_record, publish_release, publish_withdrawals,
)
from omnisignal.knowledge.replica import KnowledgeReplica


@pytest.fixture
def setup(tmp_path):
    assets = tmp_path / "assets"
    (assets / "images").mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(assets / "images/test.png")
    key = Ed25519PrivateKey.generate()
    case = {
        "case_id": "synthetic-case", "revision": 1, "title": "Synthetic packaging",
        "summary_zh": "仅测试：中文茶饮包装", "brand_or_team": "Synthetic team",
        "tags": {"industry": ["tea"], "medium": ["packaging"]},
        "review_status": "approved", "display_permission": "allowed",
        "analysis_version": "test-1", "model_version": "none",
        "sources": [{"source_id": "source-1", "url": "https://example.invalid/test", "display_permission": "allowed"}],
        "images": [{"image_id": "image-1", "path": "images/test.png", "source_id": "source-1", "display_permission": "allowed"}],
        "claims": [{"claim_id": "claim-1", "kind": "visual_observation", "text": "测试红色图块", "source_ids": ["source-1"], "image_ids": ["image-1"]}],
    }
    base = dict(root=tmp_path / "publisher", release_id="r1", sequence=1,
                records=[make_record(case)], asset_root=assets, private_key=key, key_id="test-key")
    publish_release(**base)
    replica = KnowledgeReplica(tmp_path / "replica")
    keys = {"test-key": key.public_key()}
    return base, case, replica, keys


def package(base, name="r1"):
    return base["root"] / "releases" / name


def delta(base, records, *, number=2, parent=None):
    publish_release(**{**base, "release_id": f"r{number}", "sequence": number,
                       "kind": "delta", "parent_release_id": parent or f"r{number - 1}", "records": records})
    return package(base, f"r{number}")


def resign(directory, key, **changes):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.update(changes)
    manifest.pop("signature")
    path.write_bytes(canonical(_signed(manifest, key, "test-key")))


def test_local_full_copy_survives_a_disconnect_and_restart(setup):
    base, case, replica, keys = setup
    assert replica.accept_release(package(base), keys)["status"] == "accepted"
    local_image = replica.asset_path(case["case_id"], "image-1")
    assert local_image.is_relative_to(replica.root)
    shutil.rmtree(base["root"])
    restarted = KnowledgeReplica(replica.root)
    assert restarted.query_cases() == [case]
    assert restarted.asset_path(case["case_id"], "image-1").read_bytes() == local_image.read_bytes()
    assert restarted.status()["production_nova_connected"] is False


def test_chinese_substring_and_tag_search_is_not_vector_search(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    assert replica.query_cases("茶饮", {"medium": ["packaging"]}) == [case]
    assert replica.query_cases("PACKAGING") == [case]
    assert replica.query_cases("茶饮", {"industry": ["cars"]}) == []
    assert replica.query_cases("missing") == []
    assert replica.status()["search_mode"] == "keyword_and_tags"
    with pytest.raises(KnowledgeError): replica.query_cases(tags={"unknown": ["x"]})


def test_delta_inherits_old_assets_and_deletes_only_new_view(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    other = {**copy.deepcopy(case), "case_id": "another-case"}
    replica.accept_release(delta(base, [make_record(other)]), keys)
    assert len(replica.query_cases()) == 2
    assert "/r1/" in replica.asset_path(case["case_id"], "image-1").as_posix()
    assert "/r2/" in replica.asset_path(other["case_id"], "image-1").as_posix()
    replica.accept_release(delta(base, [delete_record(case["case_id"], 2)], number=3), keys)
    assert replica.query_cases() == [other]
    assert replica.rollback("r1")["high_sequence"] == 3
    assert replica.query_cases() == [case]
    # Ordinary delta deletion is versioned; global withdrawal is not.
    assert replica.accept_release(package(base), keys)["status"] == "already_accepted"
    assert replica.status()["active_release"] == "r1"


@pytest.mark.parametrize("fault", ["image", "signature", "missing", "extra", "records"])
def test_invalid_package_never_changes_active(setup, fault):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    path = delta(base, [make_record({**case, "revision": 2})])
    if fault == "image": (path / "images/test.png").write_bytes(b"broken")
    elif fault == "signature":
        data = json.loads((path / "manifest.json").read_bytes()); data["signature"] = "invalid"
        (path / "manifest.json").write_text(json.dumps(data))
    elif fault == "missing": (path / "images/test.png").unlink()
    elif fault == "extra": (path / "unlisted.txt").write_text("unlisted")
    else: (path / "records.jsonl").write_bytes(b"{}\n")
    with pytest.raises(KnowledgeError): replica.accept_release(path, keys)
    assert replica.status()["active_release"] == "r1"
    assert replica.query_cases() == [case]


def test_independent_key_trust_required(setup):
    base, _, replica, _ = setup
    with pytest.raises(KnowledgeError, match="untrusted"):
        replica.accept_release(package(base), {})
    assert replica.status()["active_release"] is None


@pytest.mark.parametrize("mutation", ["parent", "gap", "id_conflict", "revision"])
def test_signed_but_out_of_order_or_conflicting_content_is_rejected(setup, mutation):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    path = delta(base, [make_record({**case, "revision": 2})])
    if mutation == "parent": resign(path, base["private_key"], parent_release_id="unknown")
    elif mutation == "gap": resign(path, base["private_key"], sequence=3)
    elif mutation == "id_conflict": resign(path, base["private_key"], release_id="r1")
    else:
        changed = {**case, "title": "same revision changed content"}
        records = canonical(make_record(changed)) + b"\n"
        (path / "records.jsonl").write_bytes(records)
        manifest = json.loads((path / "manifest.json").read_bytes())
        for entry in manifest["files"]:
            if entry["path"] == "records.jsonl": entry.update(size=len(records), sha256=digest(records))
        resign(path, base["private_key"], files=manifest["files"])
    with pytest.raises(KnowledgeError): replica.accept_release(path, keys)
    assert replica.status()["active_release"] == "r1"


def test_delta_cannot_bootstrap_and_full_replaces_view(setup):
    base, _, replica, keys = setup
    path = delta(base, [])
    with pytest.raises(KnowledgeError, match="parent"): replica.accept_release(path, keys)
    replica.accept_release(package(base), keys)
    replica.accept_release(path, keys)
    publish_release(**{**base, "release_id": "r3", "sequence": 3, "records": []})
    replica.accept_release(package(base, "r3"), keys)
    assert replica.query_cases() == []
    assert replica.rollback("r1")["active_release"] == "r1"
    assert len(replica.query_cases()) == 1
    fresh = KnowledgeReplica(replica.root.parent / "fresh")
    fresh.accept_release(package(base, "r3"), keys)
    assert fresh.status()["high_sequence"] == 3


def test_rollback_does_not_lower_ingest_high_water(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    replica.accept_release(delta(base, [make_record({**case, "revision": 2})]), keys)
    replica.rollback("r1")
    path = delta(base, [make_record({**case, "revision": 3})], number=3)
    replica.accept_release(path, keys)
    assert replica.query_cases()[0]["revision"] == 3
    replica.accept_release(package(base), keys)
    assert replica.status()["active_release"] == "r3"


def test_withdrawal_hides_cases_images_and_all_old_versions(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    replica.accept_release(delta(base, [make_record({**case, "revision": 2})]), keys)
    doc = publish_withdrawals(root=base["root"], sequence=1, case_ids=[case["case_id"]], private_key=base["private_key"], key_id="test-key")
    replica.apply_withdrawals(doc, keys)
    assert replica.apply_withdrawals(doc, keys)["status"] == "already_applied"
    assert replica.query_cases() == []
    replica.rollback("r1")
    assert replica.query_cases() == []
    with pytest.raises(KnowledgeError, match="withdrawn"):
        replica.asset_path(case["case_id"], "image-1")
    assert KnowledgeReplica(replica.root).query_cases() == []


def test_withdrawals_before_release_and_cumulative_anti_replay(setup):
    base, case, replica, keys = setup
    def document(sequence, ids):
        return _signed({"schema_version": 1, "sequence": sequence, "created_at": "2026-10-22T00:00:00Z", "case_ids": ids}, base["private_key"], "test-key")
    replica.apply_withdrawals(document(2, [case["case_id"]]), keys)
    replica.accept_release(package(base), keys)
    assert replica.query_cases() == []
    for bad in [document(1, [case["case_id"]]), document(2, ["other"]), document(3, ["other"])]:
        with pytest.raises(KnowledgeError): replica.apply_withdrawals(bad, keys)
    replica.apply_withdrawals(document(4, [case["case_id"], "other"]), keys)
    assert replica.status()["withdrawn_cases"] == 2
    with pytest.raises(KnowledgeError): replica.apply_withdrawals(document(5, ["other"]), {})


@pytest.mark.parametrize("change", [{"schema_version": True}, {"sequence": True}, {"created_at": "nonsenseZ"}, {"created_at": "2026-10-22T00:00:00+08:00"}, {"case_ids": ["duplicate", "duplicate"]}])
def test_signed_invalid_withdrawal_schema_is_rejected(setup, change):
    base, _, replica, keys = setup
    document = _signed({"schema_version": 1, "sequence": 1, "created_at": "2026-10-22T00:00:00Z", "case_ids": ["case-1"], **change}, base["private_key"], "test-key")
    with pytest.raises(KnowledgeError): replica.apply_withdrawals(document, keys)
    assert replica.status()["withdrawal_sequence"] == 0


def test_corrupt_inherited_asset_blocks_new_active_version(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    replica.asset_path(case["case_id"], "image-1").write_bytes(b"corrupt")
    path = delta(base, [])
    with pytest.raises(KnowledgeError, match="asset corrupted"):
        replica.accept_release(path, keys)
    assert replica.status()["active_release"] == "r1"
    assert replica.status()["high_sequence"] == 1


def test_same_revision_cannot_change_image_bytes_even_with_valid_signature(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    path = delta(base, [make_record(case)])
    Image.new("RGB", (8, 8), "blue").save(path / "images/test.png")
    changed_image = (path / "images/test.png").read_bytes()
    manifest = json.loads((path / "manifest.json").read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == "images/test.png":
            entry.update(size=len(changed_image), sha256=digest(changed_image))
    resign(path, base["private_key"], files=manifest["files"])
    with pytest.raises(KnowledgeError, match="image content conflict"):
        replica.accept_release(path, keys)
    assert replica.status()["active_release"] == "r1"
    # A genuine new case revision can change its image bytes.
    revised = canonical(make_record({**case, "revision": 2})) + b"\n"
    (path / "records.jsonl").write_bytes(revised)
    for entry in manifest["files"]:
        if entry["path"] == "records.jsonl":
            entry.update(size=len(revised), sha256=digest(revised))
    resign(path, base["private_key"], files=manifest["files"])
    replica.accept_release(path, keys)
    assert replica.query_cases()[0]["revision"] == 2
    assert replica.asset_path(case["case_id"], "image-1").read_bytes() == changed_image


def test_referenced_image_not_arbitrary_file_and_tampering_detected(setup):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    with pytest.raises(KnowledgeError): replica.asset_path(case["case_id"], "unknown")
    with pytest.raises(KnowledgeError): replica.asset_path("../r1", "image-1")
    replica.asset_path(case["case_id"], "image-1").write_bytes(b"bad")
    with pytest.raises(KnowledgeError): replica.asset_path(case["case_id"], "image-1")
    with pytest.raises(KnowledgeError): replica.rollback("r1")


def test_lock_serializes_mutations_and_readers(setup):
    base, _, replica, keys = setup
    with _lock(replica.root):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(replica.accept_release, package(base), keys)
            with pytest.raises(KnowledgeError, match="locked"): future.result()
        with pytest.raises(KnowledgeError, match="locked"): replica.query_cases()
    replica.accept_release(package(base), keys)


def test_stage_failure_is_atomic_and_retry_recovers_orphan(setup, monkeypatch):
    base, case, replica, keys = setup
    replica.accept_release(package(base), keys)
    path = delta(base, [make_record({**case, "revision": 2})])
    real = replica._verified_asset
    def fail(*args):
        raise KnowledgeError("injected smoke check failure")
    monkeypatch.setattr(replica, "_verified_asset", fail)
    with pytest.raises(KnowledgeError, match="smoke"): replica.accept_release(path, keys)
    assert replica.status()["active_release"] == "r1"
    assert replica.status()["high_sequence"] == 1
    assert replica.query_cases() == [case]
    assert (replica.root / "releases/r2").exists()
    monkeypatch.setattr(replica, "_verified_asset", real)
    replica.accept_release(path, keys)
    assert replica.query_cases()[0]["revision"] == 2
