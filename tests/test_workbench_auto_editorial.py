"""Real isolated SQLite/files/signatures with synthetic images and offline providers.

These exercise orchestration and provenance, not real-model classification quality.
"""
from copy import deepcopy
import io
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
from PIL import Image
import pytest

from omnisignal.knowledge.release import materialize_release, read_latest, verify_release
from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider, _result
from omnisignal.workbench.auto_editorial import ACTOR, AutoEditorial
from omnisignal.workbench.publishing import publish_assets, publication_snapshot_ids, publish_store_assets, withdraw_assets
from omnisignal.workbench.review import apply_automated_review, fresh_review
from omnisignal.workbench.store import Conflict, Store
from omnisignal.workbench.taxonomy import fresh_taxonomy

CONFIG = {"schema_version": 1, "enabled": True, "source_ids": ["potw", "adsofbrands", "bpando", "itsnicethat"],
          "analysis_authorized": {"policy_id": "synthetic-100-plan", "authorized_at": "2026-10-30T16:00:00+08:00", "purpose": "image_analysis_and_ai_review"},
          "auto_publish": True, "interval_seconds": 30, "max_per_tick": 100}
SUGGESTION = {"purposes": [], "kind": "reference", "reason": "图中可见简单色块和大面积留白，作为设计参考。",
    "description": "自制合成样张使用纯色色块与有序空间关系，展示基本视觉节奏。", "ocr_text": "", "brand": "",
    "design": {"palette": "画面采用纯蓝色与明亮背景形成主色对比。", "typography": "图中未体现明确的文字字体。",
               "composition": "画面中央色块占据主体，周围保留完整空间。", "hierarchy": "中央蓝色色块构成主要视觉层级与阅读重心。",
               "imagery": "画面使用平面几何色块构成主要视觉元素。", "reusable_ideas": "可借鉴集中视觉重心并保留周围空白的方法。",
               "applicability": "可用于测试视觉参考库的基本结构。", "limitations": "合成样张无法支持材料工艺、传播效果及销量判断。"},
    "taxonomy": {"tags": {"industry": ["culture"], "medium": ["poster"], "task": []},
                 "note": "图中可见几何色块与留白构成的平面画面，来源说明为自制版式实验。"}}


class OfflineProvider:
    cache_identity = "synthetic-qwen-taxonomy-v2"
    model_version = "qwen-synthetic-offline"
    def __init__(self, output=None):
        self.output, self.calls = deepcopy(output if output is not None else SUGGESTION), 0
    def analyze(self, asset, image):
        self.calls += 1
        if isinstance(self.output, Exception):
            raise self.output
        return deepcopy(self.output)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_DIR", raising=False)
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_AUTO_EDITORIAL_CONFIG", raising=False)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "500")
    key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "synthetic-signing-key.pem"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", str(key_file))
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_KEY_ID", "synthetic-key")
    store = Store(tmp_path / "store")
    provider = OfflineProvider()
    analysis = AnalysisService(store, provider)
    auto = AutoEditorial(store, analysis, config=CONFIG)
    return store, analysis, auto, provider, key


def asset(store, index=0, *, source="potw", publish=False, analysis="unknown"):
    output = io.BytesIO()
    Image.new("RGB", (100, 80), (index % 256, 40, 120)).save(output, "PNG")
    item = store.import_asset({"title": "自制合成参考", "source_id": source,
        "source_url": f"https://example.invalid/synthetic/{source}/{index}", "description": "自制版式测试，非真实素材。"}, output.getvalue())
    if publish or analysis != "unknown":
        item = store.review(item["id"], {"expected_revision": item["revision"],
            "permissions": {"display": "allowed" if publish else "unknown", "export": "allowed" if publish else "unknown", "analysis": analysis},
            "permission_note": "自制合成图片仅用于离线流程测试。"}, "synthetic-admin", "admin")
    return item


def complete(store, analysis, auto, item):
    auto.tick()
    current = store.asset(item["id"])
    job = analysis.enqueue(item["id"], current["revision"], "system:daily-pilot")
    assert analysis.run_pending()["id"] == job["id"]
    return store.asset(item["id"]), job


def test_disabled_or_invalid_configuration_performs_no_editorial_writes(setup):
    store, analysis, _, provider, _ = setup
    item = asset(store)
    assert AutoEditorial(store, analysis).tick()["status"] == "disabled"
    assert AutoEditorial(store, config={**CONFIG, "source_ids": ["unknown"]}).tick()["status"] == "invalid_config"
    assert store.asset(item["id"])["revision"] == 1 and provider.calls == 0


def test_authorization_is_source_scoped_analysis_only_and_never_overrides_denied(setup):
    store, _, auto, _, _ = setup
    allowed, denied, outside = asset(store), asset(store, 1, analysis="denied"), asset(store, 2, source="manual")
    assert auto.tick()["authorized"] == 1
    granted = store.asset(allowed["id"])
    assert granted["permissions"] == {"display": "unknown", "analysis": "allowed", "export": "unknown"}
    assert granted["analysis_authorization"]["policy_id"] == CONFIG["analysis_authorized"]["policy_id"]
    assert store.asset(denied["id"])["permissions"]["analysis"] == "denied"
    assert store.asset(outside["id"])["permissions"]["analysis"] == "unknown"
    assert auto.tick()["authorized"] == 0


def test_qwen_classification_review_keeps_permissions_and_never_claims_human_approval(setup):
    store, analysis, auto, provider, _ = setup
    item, job = complete(store, analysis, auto, asset(store))
    result = auto.tick()
    reviewed = store.asset(item["id"])
    assert result["reviewed"] == 1 and result["published"] == 0
    assert result["blocked_reasons"]["display_export_permission_required"] == 1
    assert reviewed["status"] == "accepted" and reviewed["permissions"]["display"] == "unknown"
    assert fresh_review(reviewed)["review_method"] == "automated"
    assert reviewed["reviewed_analysis"]["provenance"] == "model_automated_review"
    assert reviewed["reviewed_analysis"]["source_job_id"] == job["id"]
    assert fresh_taxonomy(reviewed)["tags"] == SUGGESTION["taxonomy"]["tags"]
    assert auto.tick()["reviewed"] == 0 and provider.calls == 1
    assert auto.status()["human_reviewed"] is None
    assert not (store.root / "published" / "latest.json").exists()


def test_allowed_purposes_publish_exact_model_taxonomy_and_only_unconfirmed_receipt(setup):
    store, analysis, auto, _, key = setup
    item, job = complete(store, analysis, auto, asset(store, publish=True))
    result = auto.tick()
    assert result["published"] == 1 and len(result["releases"]) == 1
    receipt = result["releases"][0]
    _, records = verify_release(store.root / "published" / "releases" / receipt["version"], {"synthetic-key": key.public_key()})
    case = records[0]["case"]
    assert case["analysis_version"] == "automated-design-review-1"
    assert case["model_version"] == "qwen-synthetic-offline"
    assert case["editorial"]["reviewed_analysis"]["source_job_id"] == job["id"]
    assert case["tags"] == SUGGESTION["taxonomy"]["tags"]
    assert receipt["receipt_status"] == "unconfirmed"
    assert auto.tick()["published"] == 0
    today = auto.status()["today"]
    assert today["reviewed"] == today["published"] == 1 and today["human_reviewed"] is None


@pytest.mark.parametrize("change", ["legacy", "uncertain", "weak_design", "site_only", "non_qwen"])
def test_incomplete_or_uncertain_evidence_stays_for_manual_review(setup, change):
    store, analysis, auto, provider, _ = setup
    if change == "legacy": provider.output.pop("taxonomy")
    elif change == "uncertain": provider.output["kind"] = "uncertain"
    elif change == "weak_design": provider.output["design"]["composition"] = "图中未体现"
    elif change == "site_only": provider.output["taxonomy"]["note"] = "根据来源网站归类为海报类。"
    else: provider.model_version = "other-synthetic-model"
    item, _ = complete(store, analysis, auto, asset(store, publish=True))
    assert auto.tick()["reviewed"] == 0
    assert store.asset(item["id"])["status"] == "pending"
    assert auto.tick()["reviewed"] == 0 and provider.calls == 1


def test_human_edit_or_changed_image_prevents_stale_job_acceptance(setup):
    store, analysis, auto, _, _ = setup
    item, job = complete(store, analysis, auto, asset(store))
    changed = store.review(item["id"], {"expected_revision": item["revision"], "description": "人工刚修改的内容"}, "editor", "operator")
    with pytest.raises(Conflict, match="revision_changed"):
        apply_automated_review(store, item["id"], item["revision"], job["id"])
    assert auto.tick()["reviewed"] == 0
    assert store.asset(item["id"])["description"] == changed["description"]
    second, _ = complete(store, analysis, auto, asset(store, 1))
    (store.images / second["image_file"]).write_bytes(b"changed synthetic bytes")
    assert auto.tick()["reviewed"] == 0
    assert store.asset(second["id"])["status"] == "pending"


def test_failed_jobs_are_never_retried_by_editorial_ticks(setup):
    store, analysis, auto, provider, _ = setup
    provider.output = RuntimeError("synthetic provider failure")
    item, _ = complete(store, analysis, auto, asset(store))
    for _ in range(3):
        assert auto.tick()["blocked_reasons"]["failed_job_needs_explicit_retry"] == 1
    assert provider.calls == 1 and len(analysis.jobs(item["id"])) == 1


def test_public_review_cannot_impersonate_internal_automated_service(setup):
    store, _, _, _, _ = setup
    item = asset(store)
    payload = {"expected_revision": item["revision"], "reviewed_analysis": {
        "review_method": "automated", "summary_zh": "不能伪造的自动审核来源。",
        "design": {field: {"text": text, "kind": "visual_observation"} for field, text in SUGGESTION["design"].items()}}}
    with pytest.raises(Conflict, match="整理方式"):
        store.review(item["id"], payload, "operator", "operator")


def test_crash_after_signature_replays_same_batch_before_new_arrivals(setup, monkeypatch):
    store, analysis, auto, _, _ = setup
    first, _ = complete(store, analysis, auto, asset(store, publish=True))
    audit = store.audit
    def crash(db, entity, actor, action, details=None):
        if action == "publish": raise RuntimeError("synthetic crash after signature before commit")
        return audit(db, entity, actor, action, details)
    monkeypatch.setattr(store, "audit", crash)
    with pytest.raises(RuntimeError): auto.tick()
    release_id = read_latest(store.root / "published")["release_id"]
    assert store.asset(first["id"]).get("publication_status") != "published"
    monkeypatch.setattr(store, "audit", audit)
    second = asset(store, 1, publish=True, analysis="allowed")
    analysis.enqueue(second["id"], second["revision"], "system:daily-pilot")
    analysis.run_pending()
    result = auto.tick()
    assert result["published"] == 1 and result["releases"][0]["version"] == release_id
    assert len(list((store.root / "published" / "releases").iterdir())) == 1
    assert store.asset(second["id"]).get("publication_status") != "published"
    assert auto.tick()["published"] == 1
    assert auto.status()["today"]["published"] == 2


def test_signed_publish_replay_has_no_duplicate_audit_or_count(setup):
    store, analysis, auto, _, _ = setup
    item, _ = complete(store, analysis, auto, asset(store, publish=True))
    auto.tick()
    item = store.asset(item["id"])
    receipt = publish_store_assets(store, [item["id"]], ACTOR, expected_revisions={item["id"]: item["revision"]}, automated_only=True)
    assert receipt["replayed"] is True
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='publish'").fetchone()[0] == 1


def test_legacy_id_recovery(setup):
    store, analysis, auto, _, _ = setup
    item, job = complete(store, analysis, auto, asset(store, publish=True))
    item = apply_automated_review(store, item["id"], item["revision"], job["id"])
    short, legacy = publication_snapshot_ids([item])
    assert len(short) == 35 and len(legacy) == 51
    root = store.root / "published"
    signed = publish_assets(root, [{**item, "image_path": str(store.images / item["image_file"])}], ACTOR, publication_id=legacy)
    assert signed["version"] == legacy
    recovered = publish_store_assets(store, [item["id"]], ACTOR, expected_revisions={item["id"]: item["revision"]}, automated_only=True)
    assert recovered["version"] == legacy
    assert len(list((root / "releases").iterdir())) == 1
    assert not (root / "releases" / short).exists()


def test_manual_edit_cancels_only_unsigned_pending_batch_and_does_not_starve_new_work(setup, monkeypatch):
    import omnisignal.workbench.auto_editorial as editorial
    store, analysis, auto, _, _ = setup
    item, _ = complete(store, analysis, auto, asset(store, publish=True))
    real_publish = editorial.publish_store_assets
    def fail_before_signing(*args, **kwargs): raise OSError("synthetic disk unavailable")
    monkeypatch.setattr(editorial, "publish_store_assets", fail_before_signing)
    assert auto.tick()["blocked_reasons"]["publication_blocked"] == 1
    current = store.asset(item["id"])
    store.review(item["id"], {"expected_revision": current["revision"], "status": "excluded", "reason": "Human rejected this candidate"}, "human", "operator")
    other = asset(store, 1, publish=True, analysis="allowed")
    analysis.enqueue(other["id"], other["revision"], "system:daily-pilot")
    analysis.run_pending()
    monkeypatch.setattr(editorial, "publish_store_assets", real_publish)
    assert auto.tick()["published"] == 1
    assert set(materialize_release(store.root / "published")) == {other["id"]}
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='unsigned_publication_cancelled'").fetchone()[0] == 1


def test_signed_uncommitted_changed_batch_is_preserved_for_reconciliation(setup, monkeypatch):
    store, analysis, auto, _, _ = setup
    item, _ = complete(store, analysis, auto, asset(store, publish=True))
    audit = store.audit
    def crash(db, entity, actor, action, details=None):
        if action == "publish": raise RuntimeError("synthetic crash after signature")
        return audit(db, entity, actor, action, details)
    monkeypatch.setattr(store, "audit", crash)
    with pytest.raises(RuntimeError): auto.tick()
    current = store.asset(item["id"])
    store.review(item["id"], {"expected_revision": current["revision"], "description": "Human changed the source revision after the interruption"}, "human", "operator")
    monkeypatch.setattr(store, "audit", audit)
    assert auto.tick()["blocked_reasons"]["publication_blocked"] == 1
    with store.db() as db:
        assert db.execute("SELECT value FROM meta WHERE key='auto_editorial_publication_batch'").fetchone()
        assert db.execute("SELECT count(*) FROM audit WHERE action='unsigned_publication_cancelled'").fetchone()[0] == 0
    assert len(list((store.root / "published" / "releases").iterdir())) == 1


def test_publication_batches_are_bounded_and_checkpoints_keep_inventory_and_revocations(setup, monkeypatch):
    import omnisignal.workbench.publishing as publishing
    store, analysis, auto, _, key = setup
    monkeypatch.setattr(publishing, "CHECKPOINT_CHAIN_LIMIT", 2)
    all_ids = []
    for index in range(5):
        item, _ = complete(store, analysis, auto, asset(store, index, publish=True))
        all_ids.append(item["id"])
        result = auto.tick()
        assert result["published"] == 1
        chain = publishing._verified_chain(store.root / "published", "synthetic-key", key)
        assert len(chain) <= 2
        if index == 2:
            assert chain[0][0]["kind"] == "full" and len(chain[0][1]) == 3
            withdraw_assets(store.root / "published", [store.asset(all_ids[0])], "synthetic-admin", "自制图片撤回测试")
            store.recover_withdrawals()
    active = materialize_release(store.root / "published")
    assert set(active) == set(all_ids[1:])
    latest = read_latest(store.root / "published")
    manifest, records = verify_release(store.root / "published" / "releases" / latest["release_id"], {"synthetic-key": key.public_key()})
    assert manifest["kind"] == "full" and len(records) == 4
    assert auto.status()["today"]["published"] == 5  # checkpoint copies do not increment counts


def test_26_results_create_two_packages_not_26_and_repeated_tick_does_not_clear_daily_totals(setup):
    store, analysis, auto, provider, _ = setup
    items = [asset(store, index, publish=True, analysis="allowed") for index in range(26)]
    for item in items:
        analysis.enqueue(item["id"], item["revision"], "system:daily-pilot")
        analysis.run_pending()
    first, second, third = auto.tick(), auto.tick(), auto.tick()
    assert first["reviewed"] == 26 and first["published"] == 25
    assert second["published"] == 1 and third["published"] == 0
    assert len(list((store.root / "published" / "releases").iterdir())) == 2
    assert auto.status()["today"]["reviewed"] == auto.status()["today"]["published"] == 26
    assert provider.calls == 26


def test_current_qwen_requires_strict_taxonomy_and_malformed_json_is_single_billed_request():
    missing = deepcopy(SUGGESTION); missing.pop("taxonomy")
    with pytest.raises(Conflict, match="缺少逐图分类"):
        _result(missing, "synthetic", require_taxonomy=True)
    invalid = deepcopy(SUGGESTION); invalid["taxonomy"]["tags"]["medium"] = ["website-guessed"]
    with pytest.raises(Conflict, match="字典"):
        _result(invalid, "synthetic", require_taxonomy=True)
    for response in [httpx.Response(200, json={"choices": [{"message": {"content": "{"}}]}), httpx.Response(200, json={})]:
        calls = []
        def transport(request): calls.append(request); return response
        provider = HTTPAnalysisProvider(api_key="synthetic-only", model="qwen-synthetic-offline", transport=httpx.MockTransport(transport))
        with pytest.raises(Conflict, match="格式不合格"):
            provider.analyze({"id": "synthetic", "title": "合成"}, b"synthetic-only")
        assert len(calls) == 1
