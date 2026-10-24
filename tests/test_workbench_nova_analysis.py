"""Gateway contract tests: synthetic images and mock HTTP only."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import uuid

import httpx
from PIL import Image
import pytest

from omnisignal.workbench.analysis import AnalysisService, DESIGN_FIELDS, _result
from omnisignal.workbench.nova_analysis import NovaAnalysisProvider
from omnisignal.workbench.store import Conflict, Store

RESULT = {"purposes": [], "kind": "reference", "reason": "观察：红色块。推测用途待核实。",
          "description": "合成图片", "ocr_text": "", "brand": "",
          "design": {name: "合成测试观察，非真实案例" for name in DESIGN_FIELDS}}


def context():
    return {"id": "selected", "revision": 2, "sha256": "a" * 64, "title": "合成图",
            "description": "说明", "_analysis_request_id": str(uuid.uuid4()),
            "private_other_asset": "must-not-send"}


def provider(handler, **kwargs):
    return NovaAnalysisProvider(base_url="https://nova.example.invalid", token="synthetic-service-token",
                                transport=httpx.MockTransport(handler), **kwargs)


def completed(job_id="test-job"):
    return {"id": job_id, "status": "completed", "result": {**deepcopy(RESULT), "model_version": "actual-test-vlm"}}


def test_gateway_submits_once_polls_and_preserves_actual_model():
    calls = []
    asset = context()
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-service-token"
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"ready": True, "model": "configured-vlm"})
        if request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"request_id", "source_image_id", "revision", "image_sha256", "image_base64", "title", "description"}
            assert body["request_id"] == asset["_analysis_request_id"]
            assert body["image_sha256"] == asset["sha256"]
            assert base64.b64decode(body["image_base64"]) == b"synthetic-jpeg"
            return httpx.Response(202, json={"id": "test-job", "status": "queued"})
        return httpx.Response(200, json=completed())
    client = provider(handler, sleep=lambda _: None)
    assert client.configuration()["state"] == "not_checked" and not calls
    assert client.analyze(asset, b"synthetic-jpeg") == RESULT
    assert client.model_version == "actual-test-vlm"
    assert len(calls) == 3 and sum(x.method == "POST" for x in calls) == 1
    assert client.configuration()["ready"] is True and len(calls) == 3


@pytest.mark.parametrize("url", ["http://nova.invalid", "https://user:pass@nova.invalid", "https://nova.invalid/api",
                                 "https://nova.invalid/?token=x", "https://nova.invalid/#fragment"])
def test_gateway_requires_https_origin(url):
    with pytest.raises(Conflict):
        NovaAnalysisProvider(base_url=url, token="synthetic")


@pytest.mark.parametrize("mode", ["timeout", "redirect", "oversize", "foreign-job", "remote-failure", "bad-model"])
def test_gateway_failures_never_retry_submission_or_leak_details(mode):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"ready": True, "model": "test"})
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic-secret-body")
        if mode == "redirect":
            return httpx.Response(307, headers={"location": "https://private.invalid/secret"})
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * (256 * 1024 + 1))
        if mode == "foreign-job":
            return httpx.Response(200, json={"id": "../../admin", "status": "queued"})
        if mode == "remote-failure":
            return httpx.Response(200, json={"id": "job", "status": "failed", "reason": "synthetic-secret-body"})
        result = completed()
        result["result"].pop("model_version")
        return httpx.Response(200, json=result)
    with pytest.raises(Conflict) as error:
        provider(handler).analyze(context(), b"image")
    assert "synthetic-secret" not in str(error.value)
    assert sum(request.method == "POST" for request in calls) == 1


def test_gateway_poll_deadline_does_not_submit_again():
    seconds = [0]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ready": True, "model": "test"} if request.url.path.endswith("/config")
                              else {"id": "job", "status": "running"})
    client = provider(handler, clock=lambda: seconds[0], sleep=lambda n: seconds.__setitem__(0, seconds[0] + n), wait_seconds=5)
    with pytest.raises(Conflict):
        client.analyze(context(), b"image")
    assert seconds[0] <= 5 and sum(x.method == "POST" for x in calls) == 1


def test_design_validation_rejects_unknown_fields_and_long_values():
    assert _result(RESULT, "image")["design"] == RESULT["design"]
    for invalid in ({"extra": "unsupported"}, {**RESULT["design"], "palette": "x" * 1501},
                    {**RESULT["design"], "typography": []}):
        with pytest.raises(Conflict):
            _result({**RESULT, "design": invalid}, "image")
    with pytest.raises(Conflict):
        _result({**RESULT, "model_version": "model-spoof"}, "image")


def test_service_nova_backend_persists_design_and_requires_explicit_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_ANALYSIS_BACKEND", "nova")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_NOVA_BASE_URL", "https://nova.example.invalid")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_NOVA_TOKEN", "synthetic-service-token")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "2")
    store = Store(tmp_path / "workbench")
    image = io.BytesIO()
    Image.new("RGB", (1024, 1024), "red").save(image, "PNG")
    asset = store.import_asset({"title": "合成", "source_url": "https://example.invalid/sample"}, image.getvalue())
    asset = store.review(asset["id"], {"expected_revision": 1, "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"},
                                      "permission_note": "本地合成图"}, "admin", "admin")
    service = AnalysisService(store)
    assert service.configuration()["backend"] == "nova"
    assert service.configuration()["model"] is None
    submitted = []
    def handler(request):
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"ready": True, "model": "test"})
        body = json.loads(request.content)
        submitted.append(body)
        return httpx.Response(200, json=completed())
    service.provider.transport = httpx.MockTransport(handler)
    queued = service.enqueue(asset["id"], asset["revision"], "admin")
    assert service.enqueue(asset["id"], asset["revision"], "admin")["id"] == queued["id"]
    result = service.run_pending()
    assert result["status"] == "completed"
    assert result["suggestion"]["model_version"] == "actual-test-vlm"
    assert result["suggestion"]["design"] == RESULT["design"]
    assert uuid.UUID(submitted[0]["request_id"]).hex == queued["id"]
    assert submitted[0]["image_sha256"] == hashlib.sha256(image.getvalue()).hexdigest()
    assert store.asset(asset["id"])["status"] == "pending"
    with pytest.raises(Conflict, match="再次计费"):
        service.enqueue(asset["id"], asset["revision"], "admin")
    assert len(submitted) == 1 and service.configuration()["used"] == 1
    rerun = service.enqueue(asset["id"], asset["revision"], "admin", retry=True)
    assert rerun["id"] != queued["id"]
    assert service.run_pending()["status"] == "completed"
    assert len(submitted) == 2 and service.configuration()["used"] == 2


def test_unavailable_gateway_never_submits_image():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ready": False, "model": None})
    client = provider(handler)
    with pytest.raises(Conflict):
        client.analyze(context(), b"image")
    assert len(calls) == 1 and calls[0].method == "GET"
    assert client.configuration()["state"] == "unavailable"


@pytest.mark.parametrize("lost_submission", [True, False])
def test_durable_receipt_reconciles_after_timeout_without_new_post_or_charge(tmp_path, monkeypatch, lost_submission):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "1")
    store = Store(tmp_path / "receipts")
    image = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(image, "PNG")
    asset = store.import_asset({"title": "合成", "source_url": "https://example.invalid/synthetic"}, image.getvalue())
    asset = store.review(asset["id"], {"expected_revision": 1,
        "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"},
        "permission_note": "合成测试"}, "admin", "admin")
    calls = []
    def failing(request):
        calls.append(request)
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json={"ready": True, "model": "test"})
        if request.method == "POST" and not lost_submission:
            return httpx.Response(202, json={"id": "remote-job", "status": "running"})
        raise httpx.ReadTimeout("uncertain remote receipt")
    first = AnalysisService(store, provider(failing, sleep=lambda _: None))
    queued = first.enqueue(asset["id"], asset["revision"], "admin")
    pending = first.run_pending()
    assert pending["status"] == "waiting_remote" and pending["charged"]
    assert pending["remote_request_id"] == str(uuid.UUID(queued["id"]))
    assert pending["remote_job_id"] == (None if lost_submission else "remote-job")
    assert first.enqueue(asset["id"], asset["revision"], "admin", retry=True)["id"] == queued["id"]
    assert first.run_pending() is None  # durable backoff, no busy retry loop
    changed_provider = provider(lambda request: pytest.fail("Changed gateway must not receive original task"))
    changed_provider.cache_identity = "different-gateway"
    changed_service = AnalysisService(store, changed_provider)
    assert changed_service.enqueue(asset["id"], asset["revision"], "admin", retry=True)["id"] == queued["id"]
    with store.db() as db:
        db.execute("UPDATE analysis_jobs SET next_poll_at=0 WHERE id=?", (queued["id"],))
    assert changed_service.run_pending()["status"] == "waiting_remote"
    assert changed_service.run_pending() is None
    def reconcile(request):
        calls.append(request)
        assert request.method == "GET"
        expected = "/jobs/by-request/" + pending["remote_request_id"] if lost_submission else "/jobs/remote-job"
        assert request.url.path.endswith(expected)
        return httpx.Response(200, json=completed("remote-job"))
    # Simulate restart while the already-submitted call was running.
    with store.db() as db:
        db.execute("UPDATE analysis_jobs SET status='running',next_poll_at=0 WHERE id=?", (queued["id"],))
    second = AnalysisService(store, provider(reconcile))
    assert second.jobs()[0]["status"] == "waiting_remote"
    done = second.run_pending()
    assert done["status"] == "completed" and done["remote_job_id"] == "remote-job"
    assert second.configuration()["used"] == 1
    assert sum(request.method == "POST" for request in calls) == 1


def test_unknown_remote_request_is_not_resubmitted():
    calls = []
    def missing(request):
        calls.append(request)
        return httpx.Response(404, json={"reason": "job_not_found"})
    asset = {**context(), "_analysis_reconcile": True, "_analysis_remote_job_id": None}
    from omnisignal.workbench.nova_analysis import NovaPending
    with pytest.raises(NovaPending):
        provider(missing).analyze(asset, None)
    assert len(calls) == 1 and calls[0].method == "GET"


def test_remote_definitive_failure_stops_reconciliation():
    def failed(request):
        return httpx.Response(200, json={"id": "expired-job", "status": "failed", "reason": "analysis_expired_no_retry"})
    asset = {**context(), "_analysis_reconcile": True, "_analysis_remote_job_id": "expired-job"}
    from omnisignal.workbench.nova_analysis import NovaPending
    with pytest.raises(Conflict) as error:
        provider(failed).analyze(asset, None)
    assert not isinstance(error.value, NovaPending)
