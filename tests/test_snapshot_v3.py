"""Real, local-only snapshot publisher tests. All rights/content are synthetic."""
from copy import deepcopy
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image
import pytest

from omnisignal.knowledge import snapshot_v3 as v3
from omnisignal.knowledge.release import (KnowledgeError, canonical, digest, make_record, publish_release,
    publish_withdrawals, verify_signature)

RECIPIENT = {"kind": "library", "origin": "https://synthetic.example.invalid", "library_id": "synthetic-company-library"}


def synthetic_inputs(directory, count=2):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "synthetic.png"
    Image.new("RGB", (8, 8), "red").save(image)
    sha = digest(image.read_bytes())
    image_path = "images/" + sha + ".png"
    records = []
    for index in range(count):
        case_id = "synthetic-case-" + str(index).zfill(6)
        permissions = {"display": "allowed", "analysis": "allowed", "export": "allowed"}
        case = {"case_id": case_id, "revision": 1, "title": "Synthetic test only", "summary_zh": "仅用于协议测试的合成红色图块",
            "brand_or_team": "Synthetic", "review_status": "approved", "display_permission": "allowed", "permissions": permissions,
            "analysis_version": "synthetic-manual-1", "model_version": "none", "tags": {"industry": ["food-beverage"], "medium": ["packaging"], "task": ["campaign"]},
            "sources": [{"source_id": "source-1", "url": "https://example.invalid/synthetic", "display_permission": "allowed", "permissions": permissions}],
            "images": [{"image_id": "image-1", "path": image_path, "source_id": "source-1", "display_permission": "allowed", "permissions": permissions}],
            "claims": [{"claim_id": "claim-1", "kind": "visual_observation", "text": "测试红色图块", "source_ids": ["source-1"], "image_ids": ["image-1"]}],
            "editorial": {"publication_use_basis": {"schema_version": 2, "policy_id": "synthetic-policy", "policy_sha256": "1"*64,
                "purpose": "company_internal_inspiration", "authorized_at": "2026-11-06T00:00:00.000Z", "source_id": "manual",
                "asset_id": case_id, "revision": 1, "sha256": sha, "input_binding": "2"*64, "recipient": RECIPIENT, "authorization_source": "operator_policy"}}}
        records.append(make_record(case))
    return dict(root=directory / "published", release_id="synthetic-snapshot-1", records=records, asset_sources={image_path: image},
                private_key=Ed25519PrivateKey.generate(), key_id="synthetic-key", recipient=RECIPIENT)


@pytest.fixture
def sample(tmp_path_factory):
    return synthetic_inputs(tmp_path_factory.mktemp("snapshot"))


def test_switch_defaults_off_and_invalid_switch_does_not_enable(monkeypatch):
    monkeypatch.delenv(v3.ENABLE_ENV, raising=False)
    assert v3.enabled() is False
    monkeypatch.setenv(v3.ENABLE_ENV, "maybe")
    with pytest.raises(KnowledgeError, match="switch"):
        v3.enabled()


def test_real_10000_record_snapshot_reuses_all_historical_images(tmp_path_factory):
    base = synthetic_inputs(tmp_path_factory.mktemp("s10k"), 10000)
    first = v3.publish_snapshot(**base)
    assert first["records_count"] == 10000 and first["image_count"] == 1
    verify_signature(first, {base["key_id"]: base["private_key"].public_key()})
    records, _ = v3.snapshot_inventory(base["root"], first, verify_images=True)
    assert len(records) == 10000 and all(part["records_count"] <= 500 and part["size"] <= v3.MAX_PART_BYTES for part in first["parts"])
    folder = v3.storage(base["root"])
    before_images = {path.name: path.stat().st_mtime_ns for path in (folder/"objects").iterdir()}
    before_parts = {part["prefix"]: part["sha256"] for part in first["parts"]}
    new_case = deepcopy(base["records"][0]["case"])
    new_case["case_id"] = "synthetic-added"
    new_case["editorial"]["publication_use_basis"]["asset_id"] = new_case["case_id"]
    added = make_record(new_case)
    second = v3.publish_snapshot(**{**base, "release_id": "synthetic-snapshot-2", "records": [*base["records"], added], "requested_records": [added], "asset_sources": {}})
    changed_prefixes = {part["prefix"] for part in second["parts"] if before_parts.get(part["prefix"]) != part["sha256"]}
    assert changed_prefixes == {digest(new_case["case_id"].encode())[:2]}
    assert len(list((folder/"objects").iterdir())) == 1
    assert before_images == {path.name: path.stat().st_mtime_ns for path in (folder/"objects").iterdir()}
    assert not list((folder/"releases").rglob("*.png")) and not (base["root"]/"latest.json").exists()
    assert second["records_count"] == 10001 and second["sequence"] == 2


def test_actual_parts_cross_legacy_2000_cap(tmp_path_factory):
    base = synthetic_inputs(tmp_path_factory.mktemp("s2001"), 2001)
    manifest = v3.publish_snapshot(**base)
    assert len(v3.snapshot_inventory(base["root"], manifest)[0]) == 2001


def test_adaptive_prefix_partition_splits_only_overfull_buckets(tmp_path_factory, monkeypatch):
    base = synthetic_inputs(tmp_path_factory.mktemp("split"), 600)
    monkeypatch.setattr(v3, "TARGET_PART_RECORDS", 2)
    manifest = v3.publish_snapshot(**base)
    assert any(len(part["prefix"]) > 2 for part in manifest["parts"])
    assert all(part["records_count"] <= 2 for part in manifest["parts"])
    assert len(v3.snapshot_inventory(base["root"], manifest)[0]) == 600


@pytest.mark.parametrize("limit", ["part_count", "image_budget", "disk"])
def test_resource_limits_never_sign_or_activate_partial_catalog(sample, monkeypatch, limit):
    calls = []
    monkeypatch.setattr(v3, "_signed", lambda *args: calls.append(args))
    if limit == "part_count": monkeypatch.setattr(v3, "MAX_PARTS", 1)
    if limit == "image_budget": monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_IMAGE_BYTES", "1")
    if limit == "disk":
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(v3.shutil, "disk_usage", lambda path: usage(100, 99, 1))
    with pytest.raises(KnowledgeError):
        v3.publish_snapshot(**sample)
    assert calls == [] and v3.read_snapshot_latest(sample["root"]) is None
    assert not (v3.storage(sample["root"])/"releases"/sample["release_id"]).exists()


def test_retained_history_and_unreferenced_cas_count_before_any_new_payload_write(sample, monkeypatch):
    v3.publish_snapshot(**sample)
    v3._put(sample["root"], "objects", b"unreferenced historical synthetic cache")
    retained = v3.retained_content_bytes(sample["root"])
    before = (v3.storage(sample["root"])/"latest.json").read_bytes()
    source = sample["root"].parent/"new-blue.png"
    Image.new("RGB", (8, 8), "blue").save(source)
    sha = digest(source.read_bytes())
    case = deepcopy(sample["records"][0]["case"])
    case["revision"] = case["editorial"]["publication_use_basis"]["revision"] = 2
    case["images"][0]["path"] = "images/"+sha+".png"
    case["editorial"]["publication_use_basis"]["sha256"] = sha
    # The current directory's only image fits exactly; retained history does not.
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_IMAGE_BYTES", str(source.stat().st_size))
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", str(retained + source.stat().st_size))
    writes, signs = [], []
    monkeypatch.setattr(v3, "_put", lambda *args: writes.append(args))
    monkeypatch.setattr(v3, "_signed", lambda *args: signs.append(args))
    with pytest.raises(KnowledgeError, match="retained capacity"):
        v3.publish_snapshot(**{**sample, "release_id": "new-blue-snapshot", "records": [make_record(case)],
                               "asset_sources": {case["images"][0]["path"]: source}})
    assert writes == [] and signs == [] and v3.retained_content_bytes(sample["root"]) == retained
    assert (v3.storage(sample["root"])/"latest.json").read_bytes() == before


def test_retained_manifest_exact_boundary_and_one_byte_over(sample, monkeypatch):
    first = v3.publish_snapshot(**sample)
    before = v3.retained_content_bytes(sample["root"])
    second_id = "synthetic-snapshot-2"
    planned = {**first, "release_id": second_id, "sequence": 2, "parent_release_id": sample["release_id"]}
    limit = before + len(canonical(planned)) + 1
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", str(limit))
    second = v3.publish_snapshot(**{**sample, "release_id": second_id})
    assert v3.retained_content_bytes(sample["root"]) == limit
    third = {**second, "release_id": "synthetic-snapshot-3", "sequence": 3, "parent_release_id": second_id}
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", str(limit + len(canonical(third))))
    signs = []
    monkeypatch.setattr(v3, "_signed", lambda *args: signs.append(args))
    with pytest.raises(KnowledgeError, match="retained capacity"):
        v3.publish_snapshot(**{**sample, "release_id": third["release_id"]})
    assert signs == [] and v3.read_snapshot_latest(sample["root"])["release_id"] == second_id
    assert v3.retained_content_bytes(sample["root"]) == limit


def test_failed_partial_cache_is_charged_against_retained_budget(sample, monkeypatch):
    orphan = b"synthetic interrupted write cache"
    v3._put(sample["root"], "objects", orphan)
    assert v3.retained_content_bytes(sample["root"]) == len(orphan)
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", str(len(orphan)))
    with pytest.raises(KnowledgeError, match="retained capacity"):
        v3.publish_snapshot(**sample)
    assert v3.retained_content_bytes(sample["root"]) == len(orphan)
    assert v3.read_snapshot_latest(sample["root"]) is None


@pytest.mark.parametrize("raw", ["0", "-1", "1.2", "Infinity", "107374182401"])
def test_invalid_retained_budget_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", raw)
    with pytest.raises(KnowledgeError, match="retained budget"):
        v3.retained_budget()


def test_source_change_after_retained_preflight_never_signs(sample, monkeypatch):
    usage = v3.retained_content_bytes
    source = next(iter(sample["asset_sources"].values()))
    def mutate_after_plan(root):
        result = usage(root)
        source.write_bytes(b"synthetic source changed after preflight")
        return result
    monkeypatch.setattr(v3, "retained_content_bytes", mutate_after_plan)
    signs = []
    monkeypatch.setattr(v3, "_signed", lambda *args: signs.append(args))
    with pytest.raises(KnowledgeError, match="changed after capacity preflight"):
        v3.publish_snapshot(**sample)
    assert signs == [] and v3.read_snapshot_latest(sample["root"]) is None


def test_caller_mutation_cannot_change_frozen_catalog(sample):
    original = deepcopy(sample["records"])
    def mutate_caller():
        sample["records"][0]["case"]["title"] = "mutated after planning"
    manifest = v3.publish_snapshot(**sample, before_sign=mutate_caller)
    assert v3.snapshot_inventory(sample["root"], manifest)[0] == original
    assert manifest["request_hash"] == v3.request_hash(original, sample["recipient"])


def test_interruption_after_manifest_keeps_old_pointer_and_replays_same_signature(sample, monkeypatch):
    first = v3.publish_snapshot(**sample)
    before = (v3.storage(sample["root"])/"latest.json").read_bytes()
    atomic, signed = v3._atomic_json, v3._signed
    calls = []
    def fail_pointer(path, value):
        if path.name == "latest.json":
            raise OSError("synthetic pointer interruption")
        return atomic(path, value)
    def count_sign(*args):
        calls.append(True)
        return signed(*args)
    monkeypatch.setattr(v3, "_atomic_json", fail_pointer)
    monkeypatch.setattr(v3, "_signed", count_sign)
    second_args = {**sample, "release_id": "synthetic-snapshot-2"}
    with pytest.raises(OSError, match="interruption"):
        v3.publish_snapshot(**second_args)
    assert (v3.storage(sample["root"])/"latest.json").read_bytes() == before
    monkeypatch.setattr(v3, "_atomic_json", atomic)
    second = v3.publish_snapshot(**second_args)
    assert len(calls) == 1 and second["sequence"] == first["sequence"] + 1
    assert v3.read_snapshot_latest(sample["root"])["release_id"] == second["release_id"]
    v3.publish_snapshot(**sample)
    assert v3.read_snapshot_latest(sample["root"])["release_id"] == second["release_id"]


def test_new_batch_cannot_skip_signed_release_whose_pointer_update_was_interrupted(sample, monkeypatch):
    v3.publish_snapshot(**sample)
    atomic = v3._atomic_json
    def fail_pointer(path, value):
        if path.name == "latest.json": raise OSError("synthetic interruption")
        return atomic(path, value)
    monkeypatch.setattr(v3, "_atomic_json", fail_pointer)
    second = {**sample, "release_id": "synthetic-snapshot-2"}
    with pytest.raises(OSError):
        v3.publish_snapshot(**second)
    monkeypatch.setattr(v3, "_atomic_json", atomic)
    with pytest.raises(KnowledgeError, match="prior publication recovery required"):
        v3.publish_snapshot(**{**sample, "release_id": "synthetic-snapshot-3"})
    assert v3.read_snapshot_latest(sample["root"])["release_id"] == sample["release_id"]
    v3.publish_snapshot(**second)
    third = v3.publish_snapshot(**{**sample, "release_id": "synthetic-snapshot-3"})
    assert third["sequence"] == 3
    v3.publish_snapshot(**sample)
    assert v3.read_snapshot_latest(sample["root"])["release_id"] == third["release_id"]


def test_recovery_rejects_changed_frozen_request(sample):
    v3.publish_snapshot(**sample)
    with pytest.raises(KnowledgeError, match="recovery content conflict"):
        v3.publish_snapshot(**{**sample, "requested_records": sample["records"][:1]})


def test_omitted_case_keeps_revision_high_water(sample):
    case = deepcopy(sample["records"][0]["case"])
    case["revision"] = case["editorial"]["publication_use_basis"]["revision"] = 2
    v3.publish_snapshot(**{**sample, "records": [make_record(case)]})
    v3.publish_snapshot(**{**sample, "release_id": "empty-snapshot", "records": []})
    with pytest.raises(KnowledgeError, match="revision conflict"):
        v3.publish_snapshot(**{**sample, "release_id": "rollback-snapshot", "records": sample["records"][:1]})
    assert v3.read_snapshot_latest(sample["root"])["release_id"] == "empty-snapshot"


def test_legacy_omitted_case_keeps_revision_high_water_on_migration(sample):
    case = deepcopy(sample["records"][0]["case"])
    case["revision"] = case["editorial"]["publication_use_basis"]["revision"] = 2
    assets = sample["root"].parent/"legacy-assets"
    for relative, source in sample["asset_sources"].items():
        dest = assets/relative; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(source.read_bytes())
    common = dict(root=sample["root"], asset_root=assets, private_key=sample["private_key"], key_id=sample["key_id"], recipient=RECIPIENT)
    publish_release(**common, release_id="legacy-present", sequence=1, records=[make_record(case)])
    publish_release(**common, release_id="legacy-absent", sequence=2, records=[])
    with pytest.raises(KnowledgeError, match="revision conflict"):
        v3.publish_snapshot(**{**sample, "records": sample["records"][:1]})
    assert v3.read_snapshot_latest(sample["root"]) is None


def test_tampered_image_and_part_rejected(sample):
    manifest = v3.publish_snapshot(**sample)
    _, images = v3.snapshot_inventory(sample["root"], manifest)
    sha = next(iter(images))
    image = v3.storage(sample["root"])/"objects"/sha
    original = image.read_bytes(); image.write_bytes(b"invalid")
    with pytest.raises(KnowledgeError, match="hash"):
        v3.referenced_file(sample["root"], sample["release_id"], "objects", sha)
    image.write_bytes(original)
    part = v3.storage(sample["root"])/"parts"/manifest["parts"][0]["sha256"]
    part.write_bytes(b"{}\n")
    with pytest.raises(KnowledgeError, match="hash"):
        v3.snapshot_inventory(sample["root"], manifest)


def test_unreferenced_hash_and_path_escape_not_exposed(sample):
    v3.publish_snapshot(**sample)
    v3._put(sample["root"], "objects", b"unreferenced synthetic object")
    with pytest.raises(KnowledgeError, match="not referenced"):
        v3.referenced_file(sample["root"], sample["release_id"], "objects", digest(b"unreferenced synthetic object"))
    with pytest.raises(KnowledgeError):
        v3.referenced_file(sample["root"], "../outside", "objects", "a"*64)
    with pytest.raises(KnowledgeError):
        v3.referenced_file(sample["root"], sample["release_id"], "objects", "../../secret")


def test_part_must_be_canonical_lf_even_when_hash_recomputed(sample):
    manifest = v3.publish_snapshot(**sample)
    descriptor = manifest["parts"][0]
    value = json.loads(v3._bytes(sample["root"], "parts", descriptor["sha256"], v3.MAX_PART_BYTES))
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
    with pytest.raises(KnowledgeError, match="canonical"):
        v3.validate_part(raw, {**descriptor, "sha256": digest(raw), "size": len(raw)}, manifest)


def test_frozen_request_must_be_present_in_catalog(sample):
    with pytest.raises(KnowledgeError, match="not present"):
        v3.publish_snapshot(**{**sample, "records": sample["records"][:1], "requested_records": sample["records"][1:]})


def test_animated_image_not_accepted_as_static_object(sample):
    first, second = Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")
    source = sample["root"].parent/"animated.webp"
    first.save(source, "WEBP", save_all=True, append_images=[second], duration=200, loop=0)
    sha = digest(source.read_bytes())
    case = deepcopy(sample["records"][0]["case"])
    case["images"][0]["path"] = "images/"+sha+".webp"
    case["editorial"]["publication_use_basis"]["sha256"] = sha
    with pytest.raises(KnowledgeError, match="animated"):
        v3.publish_snapshot(**{**sample, "records": [make_record(case)], "asset_sources": {case["images"][0]["path"]: source}})


def test_image_extension_must_match_actual_mime(sample):
    case = deepcopy(sample["records"][0]["case"])
    source = sample["asset_sources"][case["images"][0]["path"]]
    case["images"][0]["path"] = case["images"][0]["path"].replace(".png", ".jpg")
    with pytest.raises(KnowledgeError, match="extension/MIME"):
        v3.publish_snapshot(**{**sample, "records": [make_record(case)], "asset_sources": {case["images"][0]["path"]: source}})


def test_v2_migration_has_new_sequence_and_preserves_legacy_bytes(sample):
    assets = sample["root"].parent/"legacy-assets"
    for relative, source in sample["asset_sources"].items():
        dest = assets/relative; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(source.read_bytes())
    publish_release(root=sample["root"], release_id="legacy-1", sequence=1, records=sample["records"], asset_root=assets,
                    private_key=sample["private_key"], key_id=sample["key_id"], recipient=RECIPIENT)
    before = {path.relative_to(sample["root"]): path.read_bytes() for path in sample["root"].rglob("*") if path.is_file()}
    manifest = v3.publish_snapshot(**sample)
    assert manifest["sequence"] == 2 and manifest["parent_release_id"] == "legacy-1"
    assert all((sample["root"]/name).read_bytes() == value for name, value in before.items())


def test_disabled_snapshot_still_wins_over_older_legacy_pointer(sample, monkeypatch):
    from omnisignal.workbench.publishing import current_publication_cases, current_publication_latest
    assets = sample["root"].parent/"legacy-assets"
    for relative, source in sample["asset_sources"].items():
        dest = assets/relative; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(source.read_bytes())
    publish_release(root=sample["root"], release_id="legacy-1", sequence=1, records=sample["records"], asset_root=assets,
                    private_key=sample["private_key"], key_id=sample["key_id"], recipient=RECIPIENT)
    manifest = v3.publish_snapshot(**{**sample, "records": sample["records"][:1]})
    monkeypatch.setenv(v3.ENABLE_ENV, "false")
    assert current_publication_latest(sample["root"])["release_id"] == manifest["release_id"]
    assert set(current_publication_cases(sample["root"])) == {sample["records"][0]["case_id"]}


def test_independent_legacy_writer_cannot_conflict_with_snapshot_sequence(sample):
    from omnisignal.workbench.publishing import current_publication_cases
    v3.publish_snapshot(**sample)
    assets = sample["root"].parent/"legacy-assets"
    for relative, source in sample["asset_sources"].items():
        dest = assets/relative; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(source.read_bytes())
    common = dict(root=sample["root"], records=sample["records"], asset_root=assets,
                  private_key=sample["private_key"], key_id=sample["key_id"], recipient=RECIPIENT)
    publish_release(**common, release_id="independent-legacy-1", sequence=1)
    with pytest.raises(KnowledgeError, match="sequence conflicts"):
        current_publication_cases(sample["root"])
    publish_release(**common, release_id="independent-legacy-2", sequence=2)
    with pytest.raises(KnowledgeError, match="legacy publication advanced"):
        v3.publish_snapshot(**{**sample, "release_id": "unexpected-snapshot-3"})


def test_withdrawal_watermark_cannot_republish_case(sample):
    v3.publish_snapshot(**sample)
    publish_withdrawals(root=sample["root"], sequence=1, case_ids=[sample["records"][0]["case_id"]],
                       private_key=sample["private_key"], key_id=sample["key_id"], recipient=RECIPIENT)
    with pytest.raises(KnowledgeError, match="withdrawn"):
        v3.publish_snapshot(**{**sample, "release_id": "bad-withdrawn"})
    manifest = v3.publish_snapshot(**{**sample, "release_id": "valid-withdrawal", "records": sample["records"][1:]})
    assert manifest["withdrawal_sequence"] == 1


@pytest.mark.parametrize("mutation", ["scope", "unknown", "prefix", "count", "overlap"])
def test_manifest_and_part_tampering_rejected(sample, mutation):
    manifest = v3.publish_snapshot(**sample)
    if mutation == "scope":
        descriptor = manifest["parts"][0]
        value = json.loads(v3._bytes(sample["root"], "parts", descriptor["sha256"], v3.MAX_PART_BYTES))
        value["records"][0]["case"]["editorial"]["publication_use_basis"]["recipient"]["library_id"] = "wrong-library"
        value["records"][0] = make_record(value["records"][0]["case"])
        raw = canonical(value)+b"\n"
        with pytest.raises(KnowledgeError, match="recipient"):
            v3.validate_part(raw, {**descriptor, "sha256": digest(raw), "size": len(raw)}, manifest)
        return
    changed = deepcopy(manifest)
    if mutation == "unknown": changed["untrusted"] = True
    if mutation == "prefix": changed["parts"][0]["prefix"] = "../escape"
    if mutation == "count": changed["records_count"] += 1
    if mutation == "overlap":
        changed["parts"].append({**changed["parts"][0], "prefix": changed["parts"][0]["prefix"]+"0", "sha256": "f"*64})
        changed["parts"].sort(key=lambda part: part["prefix"])
    with pytest.raises((KnowledgeError, ValueError)):
        v3.validate_manifest(changed)


def export_fixture(directory, count=2):
    """Cross-language vectors from the actual builder; no private key exported."""
    base = synthetic_inputs(directory, count)
    # Publicly reproducible test key, never used for a real publication/account.
    base["private_key"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(digest(b"synthetic-snapshot-v3-fixture-only")))
    manifest = v3.publish_snapshot(**base)
    folder = v3.storage(base["root"])
    envelope = {"synthetic_only": True, "manifest": base64.b64encode((folder/"releases"/base["release_id"]/"manifest.json").read_bytes()).decode(),
        "parts": {part["sha256"]: base64.b64encode((folder/"parts"/part["sha256"]).read_bytes()).decode() for part in manifest["parts"]},
        "objects": {path.name: base64.b64encode(path.read_bytes()).decode() for path in (folder/"objects").iterdir()},
        "publicKey": base["private_key"].public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        "keyId": base["key_id"], "recipient": RECIPIENT}
    (Path(directory)/"fixture.json").write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return envelope
