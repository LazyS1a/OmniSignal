"""Synthetic pictures and mock transports only; no paid provider calls."""
from copy import deepcopy
import io
import json
import threading

import httpx
from PIL import Image
import pytest

from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider
from omnisignal.workbench.store import Conflict, Store


SUGGESTION = {"purposes": ["brand", "conversion"], "kind": "advertisement", "reason": "图中有品牌字样；促销目的需人工确认",
              "description": "合成图片说明", "ocr_text": "SYNTHETIC", "brand": "Synthetic"}


class FakeProvider:
    cache_identity = "synthetic-provider-v1"
    model_version = "synthetic-only"

    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback
        self.image = None

    def analyze(self, asset, image):
        self.calls += 1
        self.image = image
        if self.callback:
            return self.callback(asset, image)
        return deepcopy(SUGGESTION)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "3")
    store = Store(tmp_path / "workbench")
    data = io.BytesIO()
    Image.new("RGB", (1800, 1200), "red").save(data, "PNG")
    asset = store.import_asset({"title": "合成广告", "source_url": "https://example.invalid/case",
                                "description": "原始人工说明"}, data.getvalue())
    asset = store.review(asset["id"], {"expected_revision": 1,
                         "permissions": {"display": "unknown", "analysis": "allowed", "export": "unknown"},
                         "permission_note": "仅本地合成图片测试"}, "synthetic-admin", "admin")
    return store, asset


def test_default_budget_disables_calls_and_does_not_reuse_other_credentials(prepared, monkeypatch):
    store, asset = prepared
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT")
    service = AnalysisService(store, FakeProvider())
    assert service.configuration()["status"] == "disabled"
    with pytest.raises(Conflict, match="额度为 0"):
        service.enqueue(asset["id"], asset["revision"], "admin")
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_MODEL", raising=False)
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_MODEL_API_KEY", "synthetic-legacy-key")
    monkeypatch.setenv("OMNISIGNAL_KNOWLEDGE_MODEL", "legacy-model")
    assert AnalysisService(store).configuration()["status"] == "not_configured"


def test_analysis_is_single_image_suggestion_and_cache_does_not_recharge(prepared):
    store, asset = prepared
    provider = FakeProvider()
    service = AnalysisService(store, provider)
    job = service.enqueue(asset["id"], asset["revision"], "admin")
    assert job["status"] == "queued"
    assert service.enqueue(asset["id"], asset["revision"], "admin")["id"] == job["id"]
    assert store.asset(asset["id"])["analysis_status"] == "analyzing"
    result = service.run_pending()
    assert result["status"] == "completed" and result["charged"] is True
    assert result["suggestion"]["source_image_id"] == asset["id"]
    stored = store.asset(asset["id"])
    assert stored["analysis_status"] == "analyzed"
    for field in ("revision", "kind", "status", "permissions", "purposes", "description", "reason", "brand"):
        assert stored[field] == asset[field]
    assert stored["analysis_suggestion"]["provenance"] == "model_suggestion_requires_review"
    with Image.open(io.BytesIO(provider.image)) as image:
        assert max(image.size) <= 1024 and image.format == "JPEG"
    cached = service.enqueue(asset["id"], asset["revision"], "admin")
    assert cached["cached"] is True and cached["id"] == result["id"]
    assert service.run_pending() is None and provider.calls == 1
    assert service.configuration()["used"] == 1


def test_permission_and_revision_gate(prepared):
    store, asset = prepared
    service = AnalysisService(store, FakeProvider())
    with pytest.raises(Conflict, match="被修改"):
        service.enqueue(asset["id"], 1, "admin")
    changed = store.review(asset["id"], {"expected_revision": asset["revision"],
                           "permissions": {"display": "unknown", "analysis": "denied", "export": "unknown"},
                           "permission_note": "停止分析"}, "admin", "admin")
    with pytest.raises(Conflict, match="分析授权"):
        service.enqueue(asset["id"], changed["revision"], "admin")


def test_human_edit_during_request_makes_result_stale(prepared):
    store, asset = prepared
    def update_during_request(item, image):
        store.review(item["id"], {"expected_revision": item["revision"], "description": "人工刚修改"}, "editor", "operator")
        return deepcopy(SUGGESTION)
    service = AnalysisService(store, FakeProvider(update_during_request))
    service.enqueue(asset["id"], asset["revision"], "admin")
    result = service.run_pending()
    assert result["status"] == "stale" and result["suggestion"] is None
    stored = store.asset(asset["id"])
    assert stored["description"] == "人工刚修改" and "analysis_suggestion" not in stored
    assert stored["analysis_status"] == "not_analyzed"
    assert service.configuration()["used"] == 1


def test_edit_before_execution_does_not_charge(prepared):
    store, asset = prepared
    provider = FakeProvider()
    service = AnalysisService(store, provider)
    service.enqueue(asset["id"], asset["revision"], "admin")
    store.review(asset["id"], {"expected_revision": asset["revision"], "description": "人工改动"}, "editor", "operator")
    assert service.run_pending()["status"] == "stale"
    assert provider.calls == 0 and service.configuration()["used"] == 0


def test_failures_count_against_budget_and_require_explicit_retry(prepared):
    store, asset = prepared
    def failing(asset, image):
        raise RuntimeError("synthetic-secret-must-not-leak")
    provider = FakeProvider(failing)
    service = AnalysisService(store, provider)
    service.enqueue(asset["id"], asset["revision"], "admin")
    failed = service.run_pending()
    assert failed["status"] == "failed" and failed["charged"] is True
    assert "synthetic-secret" not in json.dumps(service.jobs())
    with pytest.raises(Conflict, match="再次计费"):
        service.enqueue(asset["id"], asset["revision"], "admin")
    retried = service.enqueue(asset["id"], asset["revision"], "admin", retry=True)
    assert retried["id"] != failed["id"]
    service.run_pending()
    assert provider.calls == 2 and service.configuration()["used"] == 2


def test_restart_marks_running_interrupted_without_automatic_retry(prepared):
    store, asset = prepared
    service = AnalysisService(store, FakeProvider())
    job = service.enqueue(asset["id"], asset["revision"], "admin")
    with store.db() as db:
        db.execute("UPDATE analysis_jobs SET status='running',charged=1 WHERE id=?", (job["id"],))
        db.execute("INSERT INTO analysis_usage VALUES(?,1)", (store.day(),))
    provider = FakeProvider()
    restarted = AnalysisService(store, provider)
    assert restarted.jobs()[0]["status"] == "interrupted"
    assert restarted.run_pending() is None and provider.calls == 0
    with pytest.raises(Conflict, match="再次计费"):
        restarted.enqueue(asset["id"], asset["revision"], "admin")
    restarted.enqueue(asset["id"], asset["revision"], "admin", retry=True)
    assert restarted.run_pending()["status"] == "completed"
    assert restarted.configuration()["used"] == 2


def test_daily_limit_is_reserved_atomically_for_queued_jobs(prepared, monkeypatch):
    store, asset = prepared
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "1")
    provider = FakeProvider()
    service = AnalysisService(store, provider)
    other_bytes = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(other_bytes, "PNG")
    other = store.import_asset({"title": "其他合成图片", "source_url": "https://example.invalid/other"}, other_bytes.getvalue())
    other = store.review(other["id"], {"expected_revision": 1, "permissions": asset["permissions"], "permission_note": "合成"}, "admin", "admin")
    service.enqueue(asset["id"], asset["revision"], "admin")
    with pytest.raises(Conflict, match="已分配"):
        service.enqueue(other["id"], other["revision"], "admin")
    assert service.configuration()["reserved"] == 1
    assert service.configuration()["remaining"] == 0
    assert service.run_pending()["status"] == "completed"
    assert service.run_pending() is None
    assert provider.calls == 1
    assert service.configuration()["status"] == "budget_exhausted"


@pytest.mark.parametrize("patch", [
    {"kind": "approved"}, {"purposes": ["promotion"]}, {"reason": ""},
    {"permissions": {"display": "allowed"}}, {"ocr_text": ["not text"]},
    {"brand": "x" * 201}, {"purposes": ["brand", "brand"]},
])
def test_malformed_model_output_never_overrides_asset(prepared, patch):
    store, asset = prepared
    provider = FakeProvider(lambda a, i: {**deepcopy(SUGGESTION), **patch})
    service = AnalysisService(store, provider)
    service.enqueue(asset["id"], asset["revision"], "admin")
    assert service.run_pending()["status"] == "failed"
    stored = store.asset(asset["id"])
    assert stored["kind"] == "uncertain" and stored["status"] == "pending"
    assert "analysis_suggestion" not in stored


def test_image_tampering_blocks_call_without_charging(prepared):
    store, asset = prepared
    provider = FakeProvider()
    service = AnalysisService(store, provider)
    service.enqueue(asset["id"], asset["revision"], "admin")
    (store.images / asset["image_file"]).write_bytes(b"altered")
    result = service.run_pending()
    assert result["status"] == "failed" and not result["charged"]
    assert provider.calls == 0 and service.configuration()["used"] == 0


def test_second_service_does_not_recover_a_still_running_request(prepared):
    store, asset = prepared
    entered, release = threading.Event(), threading.Event()
    def blocking(item, image):
        entered.set()
        assert release.wait(3)
        return deepcopy(SUGGESTION)
    provider = FakeProvider(blocking)
    service = AnalysisService(store, provider)
    service.enqueue(asset["id"], asset["revision"], "admin")
    worker = threading.Thread(target=service.run_pending)
    worker.start()
    try:
        assert entered.wait(3)
        other = AnalysisService(store, FakeProvider())
        assert other.jobs()[0]["status"] == "running"
        assert other.run_pending() is None
    finally:
        release.set()
        worker.join(3)
    assert service.jobs()[0]["status"] == "completed"
    assert provider.calls == 1 and service.configuration()["used"] == 1


def test_input_larger_than_four_mib_is_not_uploaded(prepared):
    store, asset = prepared
    provider = FakeProvider()
    service = AnalysisService(store, provider)
    service.enqueue(asset["id"], asset["revision"], "admin")
    image_path = store.images / asset["image_file"]
    image_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    job = service.run_pending()
    assert job["status"] == "failed" and not job["charged"]
    assert provider.calls == 0


def test_http_provider_sends_only_selected_context_and_safely_bounds_errors():
    calls = []
    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        assert "不可信" in body["messages"][0]["content"]
        sent = json.loads(body["messages"][1]["content"][0]["text"])
        assert sent == {"source_image_id": "selected", "title": "test", "description": "context"}
        assert "permissions" not in str(body) and "other_asset" not in str(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(SUGGESTION)}}]})
    provider = HTTPAnalysisProvider(api_key="synthetic-key", model="synthetic-model", transport=httpx.MockTransport(handle))
    result = provider.analyze({"id": "selected", "title": "test", "description": "context", "permissions": {}, "other_asset": "private"}, b"synthetic-jpeg")
    assert result == SUGGESTION and len(calls) == 1


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:pass@example.invalid", "https://example.invalid/?key=secret", "https://example.invalid/#secret"])
def test_endpoint_configuration_rejects_unsafe_urls(url):
    with pytest.raises(Conflict):
        HTTPAnalysisProvider(api_key="synthetic", model="synthetic", base_url=url)


def test_http_redirects_and_large_responses_are_not_followed():
    for response in (httpx.Response(302, headers={"Location": "https://other.invalid/"}),
                     httpx.Response(200, content=b"x" * (256 * 1024 + 1))):
        requests = []
        def handler(request):
            requests.append(request)
            return response
        provider = HTTPAnalysisProvider(api_key="synthetic", model="synthetic", transport=httpx.MockTransport(handler))
        with pytest.raises(Conflict, match="请求失败"):
            provider.analyze({"id": "sample", "title": "synthetic"}, b"test")
        assert len(requests) == 1
