"""Synthetic data stays in tmp_path; no external model calls or production content."""
import copy
import json
import sqlite3

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from omnisignal.knowledge.preparation import PreparationStore, QwenAnalysisAdapter
from omnisignal.knowledge.release import KnowledgeError, materialize_release, verify_release


@pytest.fixture
def prepared(tmp_path):
    assets = tmp_path / "inputs"
    assets.mkdir()
    Image.new("RGB", (8, 8), "red").save(assets / "sample.png")
    case = {"case_id": "synthetic", "revision": 1, "title": "Synthetic only", "brand_or_team": "Test team",
            "summary_zh": "合成测试红色包装", "tags": {"industry": ["food-beverage"], "medium": ["packaging"]},
            "permissions": {"display": "allowed", "analysis": "allowed"},
            "sources": [{"source_id": "source", "url": "https://example.invalid/test", "text": "Test description",
                         "permissions": {"display": "allowed", "analysis": "allowed"}}],
            "images": [{"image_id": "image", "source_id": "source", "path": "sample.png",
                        "permissions": {"display": "allowed", "analysis": "allowed"}}],
            "claims": [{"claim_id": "claim", "text": "红色图块", "kind": "visual_observation", "image_ids": ["image"], "source_ids": []}]}
    store = PreparationStore(tmp_path / "editorial")
    return store, case, assets


class FakeModel:
    model_version = "synthetic-model"
    cache_identity = "synthetic-provider"

    def __init__(self, case, fail=False):
        self.result = {key: case[key] for key in ("summary_zh", "tags", "claims")}
        self.calls = 0
        self.fail = fail

    def analyze(self, case, images):
        self.calls += 1
        if self.fail:
            raise RuntimeError("SENSITIVE PROVIDER ERROR")
        return copy.deepcopy(self.result)


def test_import_deduplicates_images_and_removes_forged_review(prepared):
    store, case, assets = prepared
    case.update(review_status="approved", review={"actor": "forged"})
    first = store.import_case(case, assets, actor="importer")
    assert first["status"] == "pending"
    assert "review" not in first["case"]
    assert first["case"]["permissions"]["download"] == "unknown"
    assert first["case"]["images"][0]["width"] == 8
    assert store.import_case(case, assets, actor="importer") == first
    case["case_id"] = "second"
    store.import_case(case, assets, actor="importer")
    assert len(list((store.assets / "images").iterdir())) == 1
    assert len(store.list_cases()) == 2


@pytest.mark.parametrize("change", ["escape", "decode", "reference", "duplicate", "url"])
def test_invalid_import_has_no_case(prepared, change):
    store, case, assets = prepared
    if change == "escape": case["images"][0]["path"] = "../sample.png"
    if change == "decode": (assets / "sample.png").write_bytes(b"not image")
    if change == "reference": case["images"][0]["source_id"] = "missing"
    if change == "duplicate": case["sources"] *= 2
    if change == "url": case["sources"][0]["url"] = "file:///secret"
    with pytest.raises(KnowledgeError): store.import_case(case, assets, actor="importer")
    assert not store.list_cases()


def test_review_revision_and_stale_protection(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    store.review("synthetic", "return", actor="reviewer", expected_revision=1, note="补充说明")
    result = store.revise("synthetic", {"summary_zh": "修订中文摘要"}, actor="editor", expected_revision=1)
    assert result["case"]["revision"] == 2
    assert "review" not in result["case"]
    with pytest.raises(KnowledgeError, match="stale"):
        store.review("synthetic", "approve", actor="reviewer", expected_revision=1)
    result = store.review("synthetic", "approve", actor="reviewer", expected_revision=2)
    assert result["case"]["review"]["actor"] == "reviewer"
    with pytest.raises(KnowledgeError): store.review("synthetic", "return", actor="reviewer", expected_revision=2, note="change")
    assert {item["action"] for item in store.audit()} == {"import", "review_return", "revise", "review_approve"}


@pytest.mark.parametrize("change", ["rights", "taxonomy", "claim"])
def test_review_rejects_unknown_rights_tags_or_claim_evidence(prepared, change):
    store, case, assets = prepared
    if change == "rights": case["images"][0]["permissions"]["display"] = "unknown"
    if change == "taxonomy": case["tags"]["industry"] = ["imagined"]
    if change == "claim": case["claims"][0].update(kind="author_statement")
    store.import_case(case, assets, actor="importer")
    with pytest.raises(KnowledgeError): store.review("synthetic", "approve", actor="reviewer", expected_revision=1)
    assert store.get_case("synthetic")["status"] == "pending"


def test_analysis_cached_persistent_and_never_approved(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    model = FakeModel(case)
    store.analyze("synthetic", model, actor="worker")
    assert store.get_case("synthetic")["case"]["revision"] == 2
    with pytest.raises(KnowledgeError, match="stale"):
        store.review("synthetic", "approve", actor="reviewer", expected_revision=1)
    store = PreparationStore(store.root)
    store.analyze("synthetic", model, actor="worker")
    assert model.calls == 1
    assert store.get_case("synthetic")["case"]["revision"] == 2
    assert store.get_case("synthetic")["status"] == "pending"
    assert store.tasks()[0]["status"] == "succeeded"
    model.cache_identity = "other-provider"
    store.analyze("synthetic", model, actor="worker")
    assert model.calls == 2


def test_analysis_failure_is_durable_sanitized_and_retries_bounded(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    model = FakeModel(case, fail=True)
    with pytest.raises(KnowledgeError): store.analyze("synthetic", model, actor="worker")
    assert model.calls == 1
    with pytest.raises(KnowledgeError): store.analyze("synthetic", model, actor="worker")
    assert model.calls == 1
    for _ in range(3):
        with pytest.raises(KnowledgeError): store.analyze("synthetic", model, actor="worker", retry_failed=True)
    assert model.calls == 3
    assert "SENSITIVE" not in json.dumps(store.audit() + store.tasks())


def test_external_analysis_requires_permissions(prepared):
    store, case, assets = prepared
    case["images"][0]["permissions"]["analysis"] = "unknown"
    store.import_case(case, assets, actor="importer")
    model = FakeModel(case)
    with pytest.raises(KnowledgeError, match="permission"):
        store.analyze("synthetic", model, actor="worker")
    assert model.calls == 0


def test_model_cannot_approve_or_invent_image_refs(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    model = FakeModel(case)
    model.result["review_status"] = "approved"
    with pytest.raises(KnowledgeError): store.analyze("synthetic", model, actor="worker")
    assert store.get_case("synthetic")["status"] == "pending"


def test_queue_durable_idempotent_and_stale_job_does_not_call_model(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    job = store.enqueue_analysis("synthetic", actor="operator", expected_revision=1)
    assert store.enqueue_analysis("synthetic", actor="operator", expected_revision=1)["job_id"] == job["job_id"]
    store.revise("synthetic", {"title": "Changed"}, actor="editor", expected_revision=1)
    model = FakeModel(case)
    store = PreparationStore(store.root)
    assert store.run_pending(model)["status"] == "failed"
    assert model.calls == 0
    store.enqueue_analysis("synthetic", actor="operator", expected_revision=2)
    assert store.run_pending(model)["status"] == "succeeded"
    assert store.run_pending(model) is None
    assert model.calls == 1


def test_review_to_publish_to_withdraw_roundtrip(prepared, tmp_path):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    key = Ed25519PrivateKey.generate()
    args = dict(publication_root=tmp_path / "published", release_id="r1", sequence=1,
                private_key=key, key_id="key-1", actor="publisher")
    with pytest.raises(KnowledgeError, match="approval"): store.publish(**args)
    store.review("synthetic", "approve", actor="reviewer", expected_revision=1)
    first = store.publish(**args)
    assert store.publish(**args) == first
    _, records = verify_release(args["publication_root"] / "releases/r1", {"key-1": key.public_key()})
    assert records[0]["case"]["review"]["actor"] == "reviewer"
    store.withdraw(publication_root=args["publication_root"], sequence=1, case_ids=["synthetic"], private_key=key, key_id="key-1", actor="publisher", reason="测试撤回")
    assert materialize_release(args["publication_root"]) == {}
    assert store.get_case("synthetic")["status"] == "withdrawn"
    with pytest.raises(KnowledgeError): store.revise("synthetic", {"title": "Again"}, actor="editor", expected_revision=1)


def test_adapter_restricts_model_mutations_and_keeps_key_out_of_body(prepared):
    _, case, _ = prepared
    def endpoint(request):
        assert request.headers["Authorization"] == "Bearer secret-test"
        body = json.loads(request.content)
        assert "secret-test" not in request.content.decode()
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"summary_zh": "测试", "tags": {}, "claims": []})}}]})
    adapter = QwenAnalysisAdapter(endpoint="https://example.invalid/v1", api_key="secret-test", model="test", transport=httpx.MockTransport(endpoint))
    assert adapter.analyze(case, [])["summary_zh"] == "测试"
    with pytest.raises(KnowledgeError): QwenAnalysisAdapter(endpoint="http://localhost", api_key="x", model="test")


def test_interrupted_job_needs_recovery_and_explicit_retry(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    job = store.enqueue_analysis("synthetic", actor="operator", expected_revision=1)
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE jobs SET status='running'")
    assert store.run_pending(FakeModel(case)) is None
    assert store.enqueue_analysis("synthetic", actor="operator", expected_revision=1)["job_id"] == job["job_id"]
    assert store.recover_interrupted(actor="operator") == 1
    with pytest.raises(KnowledgeError, match="explicit retry"):
        store.enqueue_analysis("synthetic", actor="operator", expected_revision=1)
    retry = store.enqueue_analysis("synthetic", actor="operator", expected_revision=1, retry_failed=True)
    assert retry["job_id"] != job["job_id"]
    assert store.run_pending(FakeModel(case))["status"] == "succeeded"


def test_analyze_before_display_grant_but_cannot_publish(prepared):
    store, case, assets = prepared
    case["permissions"]["display"] = "unknown"
    store.import_case(case, assets, actor="importer")
    result = store.analyze("synthetic", FakeModel(case), actor="worker")
    assert result["case"]["display_permission"] == "unknown"
    with pytest.raises(KnowledgeError, match="display"):
        store.review("synthetic", "approve", actor="reviewer", expected_revision=2)


def test_revision_history_retains_previous_values(prepared):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    store.revise("synthetic", {"title": "Changed"}, actor="editor", expected_revision=1)
    versions = store.history("synthetic")
    assert versions[0]["document"]["title"] == "Changed"
    assert versions[1]["document"]["title"] == "Synthetic only"


def test_edited_approved_case_cannot_publish_until_reviewed_again(prepared, tmp_path):
    store, case, assets = prepared
    store.import_case(case, assets, actor="importer")
    store.review("synthetic", "approve", actor="reviewer", expected_revision=1)
    store.revise("synthetic", {"title": "Changed"}, actor="editor", expected_revision=1)
    with pytest.raises(KnowledgeError, match="approval"):
        store.publish(publication_root=tmp_path / "published", release_id="r1", sequence=1,
                      private_key=Ed25519PrivateKey.generate(), key_id="test", actor="publisher")


def test_parent_symlink_image_rejected(prepared, tmp_path):
    store, case, assets = prepared
    link = assets / "linked"
    try:
        link.symlink_to(assets, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    case["images"][0]["path"] = "linked/sample.png"
    with pytest.raises(KnowledgeError, match="symlink"):
        store.import_case(case, assets, actor="importer")
