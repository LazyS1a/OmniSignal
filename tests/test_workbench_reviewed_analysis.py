"""Self-created images only: human design review, provenance and signed export."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image
import pytest

from omnisignal.knowledge.release import KnowledgeError, verify_release
from omnisignal.workbench.analysis import AnalysisService
from omnisignal.workbench.publishing import publish_assets
from omnisignal.workbench.review import DESIGN_FIELDS
from omnisignal.workbench.store import Conflict, Store


def taxonomy_content():
    return {"tags": {"industry": ["food-beverage"], "medium": ["packaging"], "task": []}, "note": "合成测试包装分类"}


def reviewed_content(job_id=None):
    return {"source_job_id": job_id, "summary_zh": "自制红色图片的人工核对设计摘要。",
            "design": {field: {"text": "人工核对内容：" + field,
                                "kind": "visual_observation" if index < 5 else "analysis_inference"}
                       for index, field in enumerate(DESIGN_FIELDS)}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_DIR", raising=False)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "5")
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "synthetic-only.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", str(key_path))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_KEY_ID", "synthetic-key")
    store = Store(tmp_path / "store")
    output = io.BytesIO()
    Image.new("RGB", (320, 240), "red").save(output, "PNG")
    item = store.import_asset({"title": "自制设计参考样图", "source_url": "https://example.invalid/own-image"}, output.getvalue())
    return store, item, key


def approve_reference(store, item, analysis=True, allow_export=False):
    payload = {"expected_revision": item["revision"], "status": "accepted", "kind": "reference",
               "purposes": [], "reason": "自制图片用于设计参考测试", "reviewed_taxonomy": taxonomy_content()}
    if analysis:
        payload["reviewed_analysis"] = reviewed_content()
    if allow_export:
        payload.update(permissions={"display": "allowed", "analysis": "allowed", "export": "allowed"},
                       permission_note="测试作者创建图片，仅授权自有样本验收")
    return store.review(item["id"], payload, "synthetic-reviewer", "admin")


def export(store, item):
    asset = store.asset(item["id"])
    asset["image_path"] = str(store.images / asset["image_file"])
    return publish_assets(store.root / "published", [asset], "synthetic-publisher")


def test_reference_accepted_without_advertising_purposes_or_permission_escalation(setup):
    store, item, _ = setup
    accepted = approve_reference(store, item)
    assert accepted["status"] == "accepted" and accepted["kind"] == "reference" and accepted["purposes"] == []
    assert set(accepted["permissions"].values()) == {"unknown"}
    review = accepted["reviewed_analysis"]
    assert accepted["reviewed_analysis_status"] == "current"
    assert review["asset_id"] == item["id"] and review["revision"] == accepted["revision"] == 2
    assert review["sha256"] == item["sha256"] and review["reviewed_by"] == "synthetic-reviewer"
    assert review["reviewed_at"] and review["model_version"] == "none"
    assert review["provenance"] == "manual_human_review"
    with pytest.raises(KnowledgeError, match="display and export"):
        export(store, accepted)


def test_all_eight_reviewed_claims_and_summary_enter_existing_signed_protocol(setup):
    store, item, key = setup
    accepted = approve_reference(store, item, allow_export=True)
    result = export(store, accepted)
    manifest, records = verify_release(store.root / "published" / "releases" / result["version"], {"synthetic-key": key.public_key()})
    case = records[0]["case"]
    assert manifest["schema_version"] == 1 and manifest["taxonomy_version"] == "nova-1"
    assert case["summary_zh"] == accepted["reviewed_analysis"]["summary_zh"]
    assert case["model_version"] == "none"
    assert [claim["design_field"] for claim in case["claims"]] == list(DESIGN_FIELDS)
    assert len(case["claims"]) == 8
    assert all(claim["image_ids"] == ["image-1"] and claim["source_ids"] == [] for claim in case["claims"])
    assert case["claims"][-1]["kind"] == "analysis_inference"
    assert case["editorial"]["material_kind"] == "reference"
    assert case["editorial"]["marketing_purposes"] == []
    assert "dimensions" not in {claim["claim_id"] for claim in case["claims"]}


def test_manual_foundation_publication_with_reviewed_classification_remains_supported(setup):
    store, item, key = setup
    accepted = approve_reference(store, item, analysis=False, allow_export=True)
    result = export(store, accepted)
    _, records = verify_release(store.root / "published" / "releases" / result["version"], {"synthetic-key": key.public_key()})
    assert records[0]["case"]["claims"][0]["claim_id"] == "dimensions"
    assert records[0]["case"]["summary_zh"].startswith("人工审核收录的设计参考")


def test_any_later_review_without_explicit_reapproval_makes_analysis_unpublishable(setup):
    store, item, _ = setup
    accepted = approve_reference(store, item, allow_export=True)
    updated = store.review(item["id"], {"expected_revision": accepted["revision"], "reason": "仅更新判断说明"}, "reviewer", "operator")
    assert updated["reviewed_analysis_status"] == "stale"
    assert store.asset(item["id"])["reviewed_analysis_status"] == "stale"
    assert store.list_assets()["items"][0]["reviewed_analysis_status"] == "stale"
    with pytest.raises(KnowledgeError, match="过期"):
        export(store, updated)
    assert not (store.root / "published" / "latest.json").exists()
    reapproved = store.review(item["id"], {"expected_revision": updated["revision"], "reviewed_analysis": reviewed_content(), "reviewed_taxonomy": taxonomy_content()}, "reviewer", "operator")
    assert reapproved["reviewed_analysis_status"] == "current"
    assert export(store, reapproved)["status"] == "published"


class FakeProvider:
    cache_identity = "synthetic-real-job-cache-identity"
    model_version = "synthetic-model-v1"

    def analyze(self, asset, image):
        return {"kind": "reference", "purposes": [], "reason": "仅为自制设计参考", "description": "自制红色样张",
                "ocr_text": "", "brand": "", "design": {field: "合成建议：" + field for field in DESIGN_FIELDS}}


def completed_job(store, item):
    authorized = store.review(item["id"], {"expected_revision": item["revision"],
                              "permissions": {"display": "allowed", "analysis": "allowed", "export": "allowed"},
                              "permission_note": "自有合成图片测试授权"}, "admin", "admin")
    service = AnalysisService(store, FakeProvider())
    service.enqueue(item["id"], authorized["revision"], "operator")
    job = service.run_pending()
    assert job["status"] == "completed"
    return store.asset(item["id"]), job


def test_completed_current_model_job_is_verified_and_model_identity_comes_from_server(setup):
    store, item, key = setup
    analyzed, job = completed_job(store, item)
    assert analyzed["analysis_suggestion_job_id"] == job["id"]
    accepted = store.review(item["id"], {"expected_revision": analyzed["revision"], "kind": "reference", "status": "accepted",
                            "reviewed_analysis": reviewed_content(job["id"]), "reviewed_taxonomy": taxonomy_content()}, "reviewer", "operator")
    review = accepted["reviewed_analysis"]
    assert review["provenance"] == "model_assisted_human_review"
    assert review["model_version"] == FakeProvider.model_version and review["source_job_id"] == job["id"]
    assert accepted["analysis_suggestion_job_id"] is None
    result = export(store, accepted)
    _, records = verify_release(store.root / "published" / "releases" / result["version"], {"synthetic-key": key.public_key()})
    assert records[0]["case"]["model_version"] == FakeProvider.model_version
    with pytest.raises(Conflict, match="当前版本"):
        store.review(item["id"], {"expected_revision": accepted["revision"], "reviewed_analysis": reviewed_content(job["id"])}, "reviewer", "operator")


@pytest.mark.parametrize("tamper", ["cache_hash", "status", "source_image", "model_version", "asset_id"])
def test_forged_or_wrong_job_provenance_cannot_be_attached(setup, tamper):
    store, item, _ = setup
    analyzed, job = completed_job(store, item)
    with store.db() as db:
        if tamper == "cache_hash":
            db.execute("UPDATE analysis_jobs SET cache_key='invalid' WHERE id=?", (job["id"],))
        elif tamper == "status":
            db.execute("UPDATE analysis_jobs SET status='failed' WHERE id=?", (job["id"],))
        elif tamper == "asset_id":
            db.execute("UPDATE analysis_jobs SET asset_id='other-image' WHERE id=?", (job["id"],))
        else:
            suggestion = dict(job["suggestion"])
            if tamper == "source_image":
                suggestion["source_image_id"] = "other-image"
            else:
                suggestion.pop("model_version")
            db.execute("UPDATE analysis_jobs SET suggestion=? WHERE id=?", (json.dumps(suggestion), job["id"]))
    with pytest.raises(Conflict, match="真实完成"):
        store.review(item["id"], {"expected_revision": analyzed["revision"], "reviewed_analysis": reviewed_content(job["id"])}, "reviewer", "operator")
    assert store.asset(item["id"])["revision"] == analyzed["revision"]
    assert store.asset(item["id"])["analysis_suggestion_job_id"] is None


@pytest.mark.parametrize("mutation", ["missing_field", "all_empty", "wrong_kind", "claim_author", "extra_model", "empty_summary", "too_long", "null"])
def test_review_contract_rejects_ambiguous_or_spoofed_metadata(setup, mutation):
    store, item, _ = setup
    content = reviewed_content()
    if mutation == "missing_field": content["design"].pop("palette")
    elif mutation == "all_empty":
        for value in content["design"].values(): value["text"] = "   "
    elif mutation == "wrong_kind": content["design"]["palette"]["kind"] = "model_fact"
    elif mutation == "claim_author": content["design"]["palette"]["kind"] = "author_statement"
    elif mutation == "extra_model": content["model_version"] = "spoofed-provider"
    elif mutation == "empty_summary": content["summary_zh"] = " "
    elif mutation == "too_long": content["design"]["palette"]["text"] = "字" * 1501
    elif mutation == "null": content = None
    with pytest.raises(Conflict):
        store.review(item["id"], {"expected_revision": 1, "reviewed_analysis": content}, "reviewer", "operator")
    assert store.asset(item["id"])["revision"] == 1


def test_two_reviewers_cannot_both_bind_same_revision(setup):
    store, item, _ = setup
    def submit(actor):
        try:
            return store.review(item["id"], {"expected_revision": 1, "reviewed_analysis": reviewed_content()}, actor, "operator")
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["reviewer-a", "reviewer-b"]))
    assert sum(result is not None for result in results) == 1
    final = store.asset(item["id"])
    assert final["revision"] == 2 and final["reviewed_analysis"]["revision"] == 2


def test_pending_model_suggestion_never_becomes_claim_without_explicit_review(setup):
    store, item, key = setup
    analyzed, job = completed_job(store, item)
    accepted = approve_reference(store, analyzed, analysis=False)
    result = export(store, accepted)
    _, records = verify_release(store.root / "published" / "releases" / result["version"], {"synthetic-key": key.public_key()})
    assert records[0]["case"]["model_version"] == "none"
    assert [claim["claim_id"] for claim in records[0]["case"]["claims"]] == ["dimensions"]


def test_changed_image_bytes_cannot_receive_fresh_human_review(setup):
    store, item, _ = setup
    (store.images / item["image_file"]).write_bytes(b"changed")
    with pytest.raises(Conflict, match="图片内容已变化"):
        store.review(item["id"], {"expected_revision": 1, "reviewed_analysis": reviewed_content()}, "reviewer", "operator")


def test_advertisement_still_requires_existing_advertising_purpose(setup):
    store, item, _ = setup
    with pytest.raises(Conflict, match="传播目的"):
        store.review(item["id"], {"expected_revision": 1, "kind": "advertisement", "status": "accepted", "purposes": []}, "reviewer", "operator")
    accepted = store.review(item["id"], {"expected_revision": 1, "kind": "advertisement", "status": "accepted", "purposes": ["brand", "conversion"]}, "reviewer", "operator")
    assert accepted["purposes"] == ["brand", "conversion"]
