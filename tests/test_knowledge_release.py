"""All content below is synthetic, local-only protocol test data."""
import copy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from omnisignal.knowledge.release import (
    KnowledgeError, _history, _lock, _signed, _withdrawn, canonical, delete_record, digest, load_private_key,
    make_record, materialize_release, publish_release, publish_withdrawals,
    read_latest, validate_case, verify_release, verify_signature,
)


@pytest.fixture
def setup(tmp_path):
    assets = tmp_path / "assets"
    (assets / "images").mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(assets / "images" / "test.png")
    case = {
        "case_id": "synthetic-case", "revision": 1, "title": "Synthetic test only",
        "summary_zh": "仅用于测试的中文摘要", "brand_or_team": "Synthetic test team",
        "tags": {"industry": ["test"], "medium": ["packaging"]},
        "review_status": "approved", "display_permission": "allowed",
        "analysis_version": "manual-test-1", "model_version": "none",
        "sources": [{"source_id": "source-1", "url": "https://example.invalid/test", "display_permission": "allowed"}],
        "images": [{"image_id": "image-1", "path": "images/test.png", "source_id": "source-1", "display_permission": "allowed"}],
        "claims": [{"claim_id": "claim-1", "kind": "visual_observation", "text": "测试红色图块", "source_ids": ["source-1"], "image_ids": ["image-1"]}],
    }
    key = Ed25519PrivateKey.generate()
    base = dict(root=tmp_path / "published", release_id="release-1", sequence=1,
                records=[make_record(case)], asset_root=assets, private_key=key, key_id="test-key")
    return base, case


def test_full_release_roundtrip_and_canonical_signature(setup):
    base, case = setup
    result = publish_release(**base)
    manifest, records = verify_release(base["root"] / "releases/release-1", {"test-key": base["private_key"].public_key()})
    assert result == manifest
    assert records == [make_record(case)]
    assert read_latest(base["root"])["sequence"] == 1
    assert materialize_release(base["root"]) == {case["case_id"]: case}
    assert manifest["created_at"].endswith("Z")
    assert b" " not in canonical({"b": 2, "a": 1})


@pytest.mark.parametrize("value", [1.0, 1.25, float("nan"), float("inf"), 9007199254740992,
                                   -9007199254740992, {1: "numeric-key"}, {"nested": [1.0]}, (1, 2)])
def test_canonical_rejects_cross_language_ambiguous_values(value):
    with pytest.raises(KnowledgeError):
        canonical(value)


def test_canonical_preserves_safe_integers_and_unicode_codepoint_order():
    value = {"😀": "astral", "\ue000": "bmp", "integer": 9007199254740991,
             "negative": -9007199254740991, "boolean": True, "null": None}
    result = canonical(value).decode()
    assert result.index("\ue000") < result.index("😀")
    assert "9007199254740991" in result and '"boolean":true' in result
    assert json.loads(result) == value


def test_float_metadata_cannot_be_signed_in_case_extension(setup):
    _, case = setup
    case["metadata"] = {"ambiguous_number": 1.0}
    with pytest.raises(KnowledgeError):
        make_record(case)


@pytest.mark.parametrize("field", ["source_ids", "image_ids"])
def test_evidence_arrays_must_be_explicit_even_when_other_array_has_evidence(setup, field):
    _, case = setup
    del case["claims"][0][field]
    with pytest.raises(KnowledgeError, match="explicit"):
        validate_case(case)


def test_explicit_empty_array_allowed_when_other_array_has_evidence(setup):
    _, case = setup
    case["claims"][0]["source_ids"] = []
    assert validate_case(case) == {"images/test.png"}


def test_empty_release_is_valid_and_contains_no_fake_case(setup):
    base, _ = setup
    base["records"] = []
    manifest = publish_release(**base)
    assert manifest["records_count"] == 0
    assert materialize_release(base["root"]) == {}


@pytest.mark.parametrize("field,value", [("review_status", "pending"), ("display_permission", "unknown"), ("summary_zh", "English only"), ("brand_or_team", ""), ("analysis_version", "")])
def test_minimum_publication_gate(setup, field, value):
    _, case = setup
    case[field] = value
    with pytest.raises(KnowledgeError):
        make_record(case)


@pytest.mark.parametrize("mutation", ["unknown_source", "unknown_image", "no_evidence", "no_images", "unknown_rights", "bad_path", "duplicate_id", "no_tags"])
def test_reference_and_rights_rejected(setup, mutation):
    _, case = setup
    if mutation == "unknown_source": case["images"][0]["source_id"] = "missing"
    elif mutation == "unknown_image": case["claims"][0]["image_ids"] = ["missing"]
    elif mutation == "no_evidence": case["claims"][0].update(source_ids=[], image_ids=[])
    elif mutation == "no_images": case["images"] = []
    elif mutation == "unknown_rights": case["images"][0]["display_permission"] = "unknown"
    elif mutation == "bad_path": case["images"][0]["path"] = "images/../../private.pem"
    elif mutation == "duplicate_id": case["sources"] *= 2
    elif mutation == "no_tags": case["tags"]["medium"] = []
    with pytest.raises(KnowledgeError): validate_case(case)


def test_missing_and_undecodable_image_never_updates_pointer(setup):
    base, _ = setup
    image = base["asset_root"] / "images/test.png"
    image.unlink()
    with pytest.raises(KnowledgeError): publish_release(**base)
    assert read_latest(base["root"]) is None
    image.write_bytes(b"not an image")
    with pytest.raises(KnowledgeError): publish_release(**base)
    assert list((base["root"] / "releases").iterdir()) == []


def test_idempotency_conflict_and_crash_pointer_recovery(setup):
    base, case = setup
    first = publish_release(**base)
    assert publish_release(**base) == first
    (base["root"] / "latest.json").unlink()
    assert publish_release(**base) == first
    assert read_latest(base["root"])["release_id"] == "release-1"
    case["title"] = "changed"
    with pytest.raises(KnowledgeError, match="conflict"):
        publish_release(**{**base, "records": [make_record(case)]})


def test_delta_revision_sequence_parent_and_delete(setup):
    base, case = setup
    publish_release(**base)
    case["revision"] = 2
    case["summary_zh"] = "修订测试摘要"
    delta = {**base, "release_id": "release-2", "sequence": 2, "kind": "delta", "parent_release_id": "release-1", "records": [make_record(case)]}
    with pytest.raises(KnowledgeError, match="sequence"):
        publish_release(**{**delta, "sequence": 4})
    with pytest.raises(KnowledgeError, match="parent"):
        publish_release(**{**delta, "parent_release_id": "wrong"})
    publish_release(**delta)
    assert materialize_release(base["root"])[case["case_id"]]["revision"] == 2
    assert publish_release(**base)["sequence"] == 1
    assert read_latest(base["root"])["sequence"] == 2
    with pytest.raises(KnowledgeError, match="revision"):
        publish_release(**{**delta, "release_id": "release-3", "sequence": 3, "parent_release_id": "release-2", "records": base["records"]})
    publish_release(**{**delta, "release_id": "release-3", "sequence": 3, "parent_release_id": "release-2", "records": [delete_record(case["case_id"], 3)]})
    assert materialize_release(base["root"]) == {}


def test_withdrawals_apply_to_old_versions_and_block_republication(setup):
    base, case = setup
    publish_release(**base)
    withdraw = dict(root=base["root"], sequence=1, case_ids=[case["case_id"]], private_key=base["private_key"], key_id="test-key")
    first = publish_withdrawals(**withdraw)
    verify_signature(first, {"test-key": base["private_key"].public_key()})
    assert publish_withdrawals(**withdraw) == first
    assert materialize_release(base["root"], "release-1") == {}
    case["revision"] = 2
    with pytest.raises(KnowledgeError, match="withdrawn"):
        publish_release(**{**base, "release_id": "release-2", "sequence": 2, "records": [make_record(case)]})
    with pytest.raises(KnowledgeError, match="sequence"):
        publish_withdrawals(**{**withdraw, "sequence": 3})
    second = publish_withdrawals(**{**withdraw, "sequence": 2, "case_ids": ["another-case"]})
    assert second["case_ids"] == ["another-case", "synthetic-case"]
    with pytest.raises(KnowledgeError, match="conflict"):
        publish_withdrawals(**{**withdraw, "case_ids": ["different"]})


@pytest.mark.parametrize("tamper", ["image", "record", "manifest", "extra_file", "unknown_key"])
def test_tamper_rejected(setup, tamper):
    base, _ = setup
    publish_release(**base)
    directory = base["root"] / "releases/release-1"
    keys = {"test-key": base["private_key"].public_key()}
    if tamper == "image": (directory / "images/test.png").write_bytes(b"bad")
    elif tamper == "record": (directory / "records.jsonl").write_bytes(b"{}\n")
    elif tamper == "manifest":
        value = json.loads((directory / "manifest.json").read_bytes())
        value["sequence"] = 500
        (directory / "manifest.json").write_bytes(canonical(value))
    elif tamper == "extra_file": (directory / "secret.txt").write_text("unlisted")
    elif tamper == "unknown_key": keys = {}
    with pytest.raises(KnowledgeError): verify_release(directory, keys)


def test_signing_key_must_remain_outside_publish_root(setup):
    base, _ = setup
    base["root"].mkdir()
    path = base["root"] / "private.pem"
    path.write_bytes(base["private_key"].private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(KnowledgeError, match="outside"):
        load_private_key(path, base["root"])


def test_duplicate_case_and_hash_rejected(setup):
    base, _ = setup
    with pytest.raises(KnowledgeError, match="duplicate"):
        publish_release(**{**base, "records": base["records"] * 2})
    record = copy.deepcopy(base["records"][0])
    record["content_hash"] = "0" * 64
    with pytest.raises(KnowledgeError, match="hash"):
        publish_release(**{**base, "records": [record]})


def test_single_writer_lock(setup):
    base, _ = setup
    with _lock(base["root"]):
        with pytest.raises(KnowledgeError, match="locked"):
            publish_release(**base)
    assert publish_release(**base)["sequence"] == 1


def test_symlink_asset_rejected_when_platform_permits(setup, tmp_path):
    base, _ = setup
    image = base["asset_root"] / "images/test.png"
    outside = tmp_path / "outside.png"
    outside.write_bytes(image.read_bytes())
    image.unlink()
    try:
        image.symlink_to(outside)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(KnowledgeError, match="symlink"):
        publish_release(**base)


@pytest.mark.parametrize("tamper", ["duplicate_json_key", "traversal_inventory", "wrong_count", "wrong_timestamp", "unreferenced_image"])
def test_even_signed_malformed_package_is_rejected(setup, tamper):
    base, _ = setup
    manifest = publish_release(**base)
    directory = base["root"] / "releases/release-1"
    if tamper == "duplicate_json_key":
        path = directory / "records.jsonl"
        raw = path.read_bytes().replace(b'{"case":', b'{"op":"delete","case":', 1)
        path.write_bytes(raw)
        for entry in manifest["files"]:
            if entry["path"] == "records.jsonl":
                entry.update(size=len(raw), sha256=digest(raw))
    elif tamper == "traversal_inventory": manifest["files"][0]["path"] = "../outside"
    elif tamper == "wrong_count": manifest["records_count"] = 42
    elif tamper == "wrong_timestamp": manifest["created_at"] = "not a timestamp"
    elif tamper == "unreferenced_image":
        raw = (directory / "images/test.png").read_bytes()
        (directory / "images/unused.png").write_bytes(raw)
        manifest["files"].append(dict(path="images/unused.png", size=len(raw), sha256=digest(raw)))
    manifest.pop("signature")
    (directory / "manifest.json").write_bytes(canonical(_signed(manifest, base["private_key"], "test-key")))
    with pytest.raises(KnowledgeError):
        verify_release(directory, {"test-key": base["private_key"].public_key()})


def test_record_image_and_package_size_limits(setup, monkeypatch):
    import omnisignal.knowledge.release as module
    base, _ = setup
    monkeypatch.setattr(module, "MAX_RECORD_BYTES", 30)
    with pytest.raises(KnowledgeError, match="record too large"):
        publish_release(**base)
    monkeypatch.setattr(module, "MAX_RECORD_BYTES", 1024 * 1024)
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(KnowledgeError, match="oversized image"):
        publish_release(**base)
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(module, "MAX_PACKAGE_BYTES", 20)
    with pytest.raises(KnowledgeError, match="package size"):
        publish_release(**base)
    assert read_latest(base["root"]) is None


def test_full_snapshot_omission_does_not_reset_revision_history(setup):
    base, case = setup
    case["revision"] = 5
    publish_release(**{**base, "records": [make_record(case)]})
    publish_release(**{**base, "release_id": "release-2", "sequence": 2, "records": []})
    assert materialize_release(base["root"]) == {}
    case["revision"] = 1
    with pytest.raises(KnowledgeError, match="revision"):
        publish_release(**{**base, "release_id": "release-3", "sequence": 3, "records": [make_record(case)]})


@pytest.mark.parametrize("kind", ["full", "delta"])
def test_same_case_revision_cannot_replace_image_bytes_at_same_path(setup, kind):
    base, case = setup
    publish_release(**base)
    original = (base["root"] / "releases/release-1/images/test.png").read_bytes()
    Image.new("RGB", (8, 8), "blue").save(base["asset_root"] / "images/test.png")
    next_release = {**base, "release_id": "release-2", "sequence": 2, "kind": kind,
                    "parent_release_id": "release-1" if kind == "delta" else None}
    with pytest.raises(KnowledgeError, match="image evidence changed; increase revision"):
        publish_release(**next_release)
    assert read_latest(base["root"])["sequence"] == 1
    assert not (base["root"] / "releases/release-2").exists()
    assert list(base["root"].glob(".staging-*")) == []
    assert (base["root"] / "releases/release-1/images/test.png").read_bytes() == original
    case["revision"] = 2
    result = publish_release(**{**next_release, "records": [make_record(case)]})
    assert result["sequence"] == 2
    assert materialize_release(base["root"])[case["case_id"]]["revision"] == 2


def test_same_case_revision_with_identical_image_bytes_remains_idempotent_across_releases(setup):
    base, _ = setup
    publish_release(**base)
    result = publish_release(**{**base, "release_id": "release-2", "sequence": 2})
    assert result["sequence"] == 2


def test_full_snapshot_omission_does_not_forget_prior_image_fingerprint(setup):
    base, _ = setup
    publish_release(**base)
    publish_release(**{**base, "release_id": "release-2", "sequence": 2, "records": []})
    Image.new("RGB", (8, 8), "blue").save(base["asset_root"] / "images/test.png")
    with pytest.raises(KnowledgeError, match="image evidence changed; increase revision"):
        publish_release(**{**base, "release_id": "release-3", "sequence": 3})
    assert read_latest(base["root"])["sequence"] == 2


@pytest.mark.parametrize("linked_part", ["withdrawal_directory", "withdrawal_file", "release_directory", "release_child", "release_manifest", "release_records"])
def test_local_history_and_withdrawals_reject_symlink_components(setup, tmp_path, linked_part):
    base, _ = setup
    publish_release(**base)
    outside = tmp_path / "external"
    outside.mkdir()
    if linked_part.startswith("withdrawal"):
        (outside / "1.json").write_text('{"case_ids":["synthetic-case"]}')
        destination = base["root"] / "withdrawals"
        if linked_part == "withdrawal_file":
            destination.mkdir()
            destination = destination / "1.json"
            target, is_directory = outside / "1.json", False
        else:
            target, is_directory = outside, True
        reader = lambda: _withdrawn(base["root"])
    else:
        if linked_part == "release_directory":
            destination = base["root"] / "releases"
            target = outside / "releases"
        elif linked_part == "release_child":
            destination = base["root"] / "releases/release-1"
            target = outside / "release-1"
        else:
            name = "manifest.json" if linked_part == "release_manifest" else "records.jsonl"
            destination = base["root"] / "releases/release-1" / name
            target = outside / name
        destination.rename(target)
        is_directory = target.is_dir()
        reader = lambda: _history(base["root"])
    try:
        destination.symlink_to(target, target_is_directory=is_directory)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(KnowledgeError, match="symlink"):
        reader()
