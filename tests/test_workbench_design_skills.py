"""Synthetic source data and transports: these tests make no paid model calls."""
from copy import deepcopy
import io
import json

import httpx
from PIL import Image
import pytest

from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider
from omnisignal.workbench import design_skills as skills
from omnisignal.workbench.store import Conflict, Store, encode


def envelope(profile="packaging-analysis"):
    design = {"palette": "画面使用绿色和白色两块主色形成明确对照。", "typography": "标题使用粗体无衬线字形而辅助文字明显较细。",
              "composition": "主体位于中央位置并在左右留出等量空白。", "hierarchy": "顶端标题字号最大而下方说明文字字号较小。",
              "imagery": "画面中心呈现一个方形容器和矩形纯色色块。", "reusable_ideas": "保持中央识别位置固定并用主色区分系列。",
              "applicability": "可用于需要区分不同口味的包装系列方案。", "limitations": "图片无法确认实际包装材质以及生产工艺。"}
    medium = {"packaging-analysis": "packaging", "brand-visual-analysis": "identity", "static-ad-analysis": "poster", "general-analysis": "editorial"}[profile]
    result = {"purposes": ["brand"], "kind": "reference", "reason": "图中可见容器正面文字与色块；系列变化属于设计推断。", "description": "本图以绿色与白色构成主体容器和文字层级。",
              "ocr_text": "", "brand": "", "design": design, "taxonomy": {"tags": {"industry": ["food-beverage"], "medium": [medium], "task": []}, "note": "图中可见容器主体和正面文字层级，来源说明仅作为辅助。"}}
    details = {"profile": profile, "route_reason": "本图以容器正面为主体，依据图中结构选择对应主媒介。",
               "observations": [{"id": f"o{i+1}", "field": field} for i, field in enumerate(skills.OBSERVATION_FIELDS)],
               "source_statements": [], "inferences": [{"field": field, "evidence_ids": ["o1", "o3"]} for field in skills.INFERENCE_FIELDS],
               "unknowns": ["实际工艺未知"], "inspiration_card": {"task": "用主色区分系列包装", "evidence_ids": ["o1", "o3"]}, "self_check": []}
    return {"result": result, "details": details}


class SkillsProvider:
    cache_identity = "design-v1:synthetic"
    model_version = "qwen-synthetic"
    requires_taxonomy = True
    last_usage = None

    def __init__(self, output=None, callback=None):
        self.output = envelope() if output is None else output
        self.callback = callback
        self.calls = 0

    def analyze(self, asset, image):
        self.calls += 1
        if self.callback:
            self.callback(asset)
        return deepcopy(self.output)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "20")
    store = Store(tmp_path / "workbench")
    output = io.BytesIO()
    Image.new("RGB", (128, 128), "green").save(output, "PNG")
    asset = store.import_asset({"title": "自有合成包装", "description": "自有测试案例", "source_url": "https://example.invalid/test"}, output.getvalue())
    asset = store.review(asset["id"], {"expected_revision": asset["revision"], "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"}, "permission_note": "自有合成测试"}, "test-admin", "admin")
    return store, asset


def completed(prepared, output=None):
    store, asset = prepared
    provider = SkillsProvider(output)
    service = AnalysisService(store, provider)
    job = service.enqueue(asset["id"], asset["revision"], "test-admin")
    result = service.run_pending()
    return store, asset, provider, service, job, result


@pytest.mark.parametrize("profile", list(skills.PROFILES))
def test_profiles_store_separate_details_preserve_legacy_projection(prepared, profile):
    store, asset, provider, service, job, result = completed(prepared, envelope(profile))
    assert result["status"] == "completed", result
    assert result["details"]["profile"] == profile
    assert result["details"]["quality"]["status"] == ("needs_attention" if profile == "general-analysis" else "passed")
    assert result["details"]["source"]["sha256"] == asset["sha256"]
    assert "details" not in result["suggestion"]
    assert "details" not in store.asset(asset["id"])["analysis_suggestion"]
    assert result["details"]["usage"] is None
    assert provider.calls == 1
    assert service.enqueue(asset["id"], asset["revision"], "test-admin")["cached"]
    assert provider.calls == 1
    with store.db() as db:
        row = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone())
        if profile == "general-analysis":
            with pytest.raises(Conflict):
                skills.require_publishable_details(db, row, asset)
        else:
            assert skills.require_publishable_details(db, row, {**asset, "revision": asset["revision"] + 1}) == result["details"]


@pytest.mark.parametrize("change", ["wrong_quote", "wrong_ref", "projection_conflict", "permission_injection", "missing_details"])
def test_invalid_model_response_fails_once_without_retry(prepared, change):
    value = envelope()
    if change == "wrong_quote":
        value["details"]["source_statements"] = [{"text": "来源声称获奖", "source_quote": "获奖作品"}]
    elif change == "wrong_ref":
        value["details"]["inferences"][0]["evidence_ids"] = ["other-image"]
    elif change == "projection_conflict":
        value["details"]["observations"][0]["text"] = "错误的另一个分析投影"
    elif change == "permission_injection":
        value["details"]["display_permission"] = "allowed"
    else:
        value = value["result"]
    store, asset, provider, service, job, result = completed(prepared, value)
    assert result["status"] == "failed"
    assert result["failure_code"] == "response_schema_invalid"
    assert result["details"] is None
    assert provider.calls == 1 and service.run_pending() is None
    assert store.asset(asset["id"])["permissions"]["display"] == "unknown"


@pytest.mark.parametrize("change,code", [("profile", "profile_taxonomy_conflict"), ("brand", "brand_without_visible_text"), ("self_check", "model_reported_issue"), ("effect", "unsupported_specific_claim")])
def test_content_issues_hold_without_technical_failure(prepared, change, code):
    value = envelope()
    if change == "profile":
        value["details"]["profile"] = "brand-visual-analysis"
    elif change == "brand":
        value["result"]["brand"] = "Invented Brand"
    elif change == "self_check":
        value["details"]["self_check"] = [{"field": "composition", "evidence": "主体位置前后描述矛盾", "action": "对照当前图检查位置"}]
    else:
        value["result"]["design"]["imagery"] = "采用烫金工艺可以提高转化率百分之二十。"
    *_, result = completed(prepared, value)
    assert result["status"] == "completed"
    assert result["details"]["quality"]["status"] == "needs_attention"
    assert code in {x["code"] for x in result["details"]["quality"]["issues"]}


def test_details_digest_missing_or_tampered_fail_closed(prepared):
    store, asset, provider, service, job, result = completed(prepared)
    with store.db() as db:
        row = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone())
        changed = deepcopy(result["details"])
        changed["source_statements"] = [{"text": "新增伪造", "source_quote": "不存在的证据"}]
        db.execute("UPDATE analysis_details SET document=? WHERE job_id=?", (encode(changed), job["id"]))
        with pytest.raises(Conflict):
            skills.require_publishable_details(db, row, asset)
        db.execute("DELETE FROM analysis_details WHERE job_id=?", (job["id"],))
        with pytest.raises(Conflict):
            skills.require_publishable_details(db, row, asset)
        assert skills.require_publishable_details(db, {**row, "provider": "legacy"}, asset) is None


def test_input_changes_keep_paid_details_but_stale_result(prepared):
    store, asset = prepared
    def change(current):
        store.review(asset["id"], {"expected_revision": asset["revision"], "description": "新的素材说明"}, "test-admin", "admin")
    service = AnalysisService(store, SkillsProvider(callback=change))
    service.enqueue(asset["id"], asset["revision"], "test-admin")
    result = service.run_pending()
    assert result["status"] == "stale" and result["details"] is not None
    assert "analysis_suggestion" not in store.asset(asset["id"])


def test_prompt_versions_bind_identity_and_usage_from_provider_metadata(monkeypatch):
    bodies = []
    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": encode(envelope())}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579, "account": "never-store"}})
    args = {"api_key": "test-only", "model": "qwen-synthetic", "transport": httpx.MockTransport(respond)}
    legacy = HTTPAnalysisProvider(**args)
    provider = HTTPAnalysisProvider(**args, design_skills=True)
    assert provider.cache_identity.startswith("design-v1:") and legacy.cache_identity != provider.cache_identity
    monkeypatch.setattr(skills, "VALIDATOR_VERSION", "changed")
    assert HTTPAnalysisProvider(**args, design_skills=True).cache_identity != provider.cache_identity
    assert HTTPAnalysisProvider(**args).cache_identity == legacy.cache_identity
    assert provider.analyze({"id": "one", "title": "合成"}, b"synthetic")["details"]["profile"] == "packaging-analysis"
    assert len(bodies) == 1 and bodies[0]["max_tokens"] == 10000
    assert provider.last_usage == {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579}
    assert "general-analysis" in bodies[0]["messages"][0]["content"]
    assert "返回且仅返回一个 JSON 对象，不得包裹代码块或附加说明。字段必须且仅为" not in bodies[0]["messages"][0]["content"]
    assert "result 对象的字段必须且仅为" in bodies[0]["messages"][0]["content"]


def test_compact_output_expands_without_repeating_model_text():
    value = envelope()
    expanded = skills.normalize_model_details(value["details"], value["result"])
    assert expanded["observations"][0]["text"] == value["result"]["design"]["palette"]
    assert expanded["inferences"][0]["text"] == value["result"]["design"]["reusable_ideas"]
    assert expanded["inspiration_card"]["method"] == value["result"]["design"]["reusable_ideas"]
    skills.validate_details(expanded, value["result"], {"title": "", "description": ""})
    expanded["observations"][0]["text"] = "更换为其他图文字"
    with pytest.raises(Conflict):
        skills.validate_details(expanded, value["result"], {"title": "", "description": ""})


@pytest.mark.parametrize("shape,code", [("nested", "envelope_nested_details"), ("flat", "envelope_flat_details")])
def test_unambiguous_envelope_layout_is_normalized(prepared, shape, code):
    value = envelope()
    value = {"result": {**value["result"], "details": value["details"]}} if shape == "nested" else {**value["result"], "details": value["details"]}
    store, asset, _, _, job, result = completed(prepared, value)
    assert result["status"] == "completed"
    assert result["details"]["system_normalizations"] == [code]
    assert result["details"]["quality"]["status"] == "passed"
    with store.db() as db:
        row = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone())
        assert skills.require_publishable_details(db, row, asset) is not None


@pytest.mark.parametrize("shape", ["regular", "nested", "flat"])
def test_missing_taxonomy_keeps_analysis_but_never_publishes(prepared, shape):
    value = envelope()
    del value["result"]["taxonomy"]
    if shape == "nested":
        value = {"result": {**value["result"], "details": value["details"]}}
    elif shape == "flat":
        value = {**value["result"], "details": value["details"]}
    store, asset, provider, service, job, result = completed(prepared, value)
    assert result["status"] == "completed" and provider.calls == 1
    assert result["suggestion"]["kind"] == "uncertain"
    assert result["suggestion"]["taxonomy"]["tags"] == {"industry": [], "medium": [], "task": []}
    assert result["suggestion"]["design"] == envelope()["result"]["design"]
    assert "taxonomy_missing" in result["details"]["system_normalizations"]
    assert "taxonomy_missing" in {x["code"] for x in result["details"]["quality"]["issues"]}
    assert result["details"]["quality"]["status"] == "needs_attention"
    with store.db() as db:
        row = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone())
        with pytest.raises(Conflict):
            skills.require_publishable_details(db, row, asset)


@pytest.mark.parametrize("change", ["permission", "duplicate_details", "missing_design", "model_normalization", "invalid_taxonomy"])
def test_envelope_normalization_does_not_accept_injection_or_ambiguity(prepared, change):
    value = envelope()
    if change == "permission":
        value["result"]["permissions"] = {"display": "allowed"}
    elif change == "duplicate_details":
        value["result"]["details"] = deepcopy(value["details"])
    elif change == "missing_design":
        del value["result"]["design"]
    elif change == "model_normalization":
        value["details"]["system_normalizations"] = []
    else:
        value["result"]["taxonomy"] = None
    *_, result = completed(prepared, value)
    assert result["status"] == "failed" and result["failure_code"] == "response_schema_invalid"


def test_brand_already_left_empty_is_warning_but_nonempty_claim_still_blocks(prepared):
    value = envelope()
    value["details"]["self_check"] = [{"field": "brand", "evidence": "图中未出现可辨识的品牌名称或商标", "action": "保持brand字段为空"}]
    *_, result = completed(prepared, value)
    quality = result["details"]["quality"]
    assert quality["status"] == "passed"
    assert quality["issues"][0]["code"] == "brand_unknown_retained" and quality["issues"][0]["severity"] == "warning"
    canonical = skills.normalize_model_details(value["details"], value["result"])
    value["result"]["brand"] = "Made Up"
    quality = skills.validate_details(canonical, value["result"], {"title": "", "description": ""})["quality"]
    assert quality["status"] == "needs_attention"
    assert {x["code"] for x in quality["issues"]} >= {"brand_without_visible_text", "model_reported_issue"}
