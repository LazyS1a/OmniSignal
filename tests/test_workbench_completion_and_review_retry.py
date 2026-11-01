"""Synthetic HTTP responses and local image faults; no real model or remote data."""
import hashlib
import io
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider
from omnisignal.workbench.auto_editorial import AutoEditorial
from omnisignal.workbench.store import Store
from test_workbench_auto_editorial import CONFIG, SUGGESTION, OfflineProvider


PRIVATE = "synthetic upstream refusal and file path must not enter stored diagnostics"
MISSING = object()


def prepared(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "100")
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_DIR", raising=False)
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_AUTO_EDITORIAL_CONFIG", raising=False)
    now = [1700000000.0]
    store = Store(tmp_path / "store", clock=lambda: now[0])
    raw = io.BytesIO()
    Image.new("RGB", (100, 80), "blue").save(raw, "PNG")
    item = store.import_asset({"title": "离线合成资料", "source_id": "potw",
                               "source_url": "https://example.invalid/synthetic", "description": "仅验证程序行为。"}, raw.getvalue())
    item = store.review(item["id"], {"expected_revision": item["revision"],
        "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"},
        "permission_note": "离线合成素材"}, "synthetic-admin", "admin")
    analysis = AnalysisService(store, provider)
    config = {**CONFIG, "analysis_authorized": None, "auto_publish": False}
    return store, item, analysis, AutoEditorial(store, analysis, config=config), now, config


@pytest.mark.parametrize("finish,refusal,valid_content,code", [
    ("length", None, True, "response_truncated"),
    ("length", None, False, "response_truncated"),
    ("content_filter", None, True, "response_refused"),
    ("stop", PRIVATE, True, "response_refused"),
    ("stop", PRIVATE, False, "response_refused"),
    ("tool_calls", None, True, "response_schema_invalid"),
    (PRIVATE, None, True, "response_schema_invalid"),
    ([], None, True, "response_schema_invalid"),
    ("stop", {"reason": PRIVATE}, True, "response_schema_invalid"),
])
def test_incomplete_or_refused_envelope_cannot_become_reviewed_success(tmp_path, monkeypatch, finish, refusal, valid_content, code):
    calls = []
    def handle(request):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"finish_reason": finish,
            "message": {"content": json.dumps(SUGGESTION) if valid_content else "{", "refusal": refusal}}]})
    provider = HTTPAnalysisProvider(api_key=PRIVATE, model="qwen-synthetic-only", transport=httpx.MockTransport(handle))
    store, item, analysis, auto, _, _ = prepared(tmp_path, monkeypatch, provider)
    analysis.enqueue(item["id"], item["revision"], "synthetic")
    result = analysis.run_pending()
    assert result["status"] == "failed" and result["failure_code"] == code
    assert result["suggestion"] is None and result["charged"]
    assert auto.tick()["reviewed"] == auto.tick()["published"] == 0
    assert analysis.run_pending() is None and len(calls) == analysis.configuration()["used"] == 1
    with store.db() as db:
        saved = str([tuple(row) for row in db.execute("SELECT reason,suggestion FROM analysis_jobs")])
        audits = str([tuple(row) for row in db.execute("SELECT details FROM audit")])
    assert PRIVATE not in saved + audits + json.dumps(analysis.jobs())
    assert store.asset(item["id"])["status"] == "pending"


@pytest.mark.parametrize("finish", [MISSING, None, "stop"])
def test_missing_null_or_normal_stop_keeps_existing_provider_compatibility(tmp_path, monkeypatch, finish):
    calls = []
    def handle(request):
        calls.append(1)
        choice = {"message": {"content": json.dumps(SUGGESTION)}}
        if finish is not MISSING:
            choice["finish_reason"] = finish
        return httpx.Response(200, json={"choices": [choice]})
    provider = HTTPAnalysisProvider(api_key="synthetic-only", model="qwen-synthetic-only", transport=httpx.MockTransport(handle))
    store, item, analysis, auto, _, _ = prepared(tmp_path, monkeypatch, provider)
    analysis.enqueue(item["id"], item["revision"], "synthetic")
    assert analysis.run_pending()["status"] == "completed"
    assert auto.tick()["reviewed"] == 1
    assert len(calls) == 1 and analysis.run_pending() is None
    assert store.asset(item["id"])["permissions"] == item["permissions"]


def completed(tmp_path, monkeypatch):
    provider = OfflineProvider()
    values = prepared(tmp_path, monkeypatch, provider)
    store, item, analysis, _, _, _ = values
    job = analysis.enqueue(item["id"], item["revision"], "synthetic")
    assert analysis.run_pending()["status"] == "completed"
    return (*values, provider, job)


def image_failure(monkeypatch, store, item, *, remaining=None):
    target = store.images / item["image_file"]
    original, calls = Path.read_bytes, []
    def read(path):
        if path == target and (remaining is None or len(calls) < remaining):
            calls.append(1)
            raise PermissionError(PRIVATE)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    return calls


def test_one_local_read_failure_can_recover_without_model_retry_or_quality_poison(tmp_path, monkeypatch):
    store, item, analysis, auto, now, config, provider, job = completed(tmp_path, monkeypatch)
    faults = image_failure(monkeypatch, store, item, remaining=1)
    first = auto.tick()
    assert first["blocked_reasons"] == {"local_review_io_retry_wait": 1}
    assert first["published"] == first["reviewed"] == 0
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM auto_editorial_outcomes").fetchone()[0] == 0
    now[0] += 59
    assert auto.tick()["reviewed"] == 0
    now[0] += 1
    restarted = AutoEditorial(store, analysis, config=config)
    assert restarted.tick()["reviewed"] == 1
    assert len(faults) == 1 and provider.calls == 1 and analysis.run_pending() is None
    current = store.asset(item["id"])
    assert current["status"] == "accepted" and current["permissions"] == item["permissions"]
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM auto_editorial_local_retries").fetchone()[0] == 0
        audit = str([tuple(row) for row in db.execute("SELECT details FROM audit WHERE action='automated_review_io_retry'")])
    assert PRIVATE not in audit and job["id"] in audit
    assert not (store.root / "published" / "latest.json").exists()


def test_local_retry_budget_survives_restart_and_stops_after_three_io_attempts(tmp_path, monkeypatch):
    store, item, analysis, auto, now, config, provider, _ = completed(tmp_path, monkeypatch)
    faults = image_failure(monkeypatch, store, item)
    assert auto.tick()["blocked_reasons"] == {"local_review_io_retry_wait": 1}
    for _ in range(4):
        assert auto.tick()["reviewed"] == 0
    assert len(faults) == 1
    now[0] += 60
    auto = AutoEditorial(store, analysis, config=config)
    assert auto.tick()["blocked_reasons"] == {"local_review_io_retry_wait": 1}
    now[0] += 299
    assert auto.tick()["reviewed"] == 0 and len(faults) == 2
    now[0] += 1
    assert auto.tick()["blocked_reasons"] == {"local_review_io_retry_exhausted": 1}
    now[0] += 86400
    assert AutoEditorial(store, analysis, config=config).tick()["blocked_reasons"] == {"local_review_io_retry_exhausted": 1}
    assert len(faults) == 3 and provider.calls == 1 and analysis.run_pending() is None
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM auto_editorial_outcomes").fetchone()[0] == 0
        assert db.execute("SELECT attempts FROM auto_editorial_local_retries").fetchone()[0] == 3
    assert store.asset(item["id"])["status"] == "pending"


def test_io_retry_rechecks_image_and_never_bypasses_quality_gates(tmp_path, monkeypatch):
    store, item, analysis, auto, now, _, provider, _ = completed(tmp_path, monkeypatch)
    image_failure(monkeypatch, store, item, remaining=1)
    assert auto.tick()["reviewed"] == 0
    (store.images / item["image_file"]).write_bytes(b"changed synthetic image")
    now[0] += 60
    assert auto.tick()["blocked_reasons"] == {"manual_review_required": 1}
    assert auto.tick()["reviewed"] == 0 and provider.calls == 1
    with store.db() as db:
        assert db.execute("SELECT outcome FROM auto_editorial_outcomes").fetchone()[0] == "manual_required"
    assert store.asset(item["id"])["status"] == "pending"


def test_old_manual_required_outcomes_are_not_reclassified_or_reopened(tmp_path, monkeypatch):
    store, item, analysis, auto, _, _, provider, job = completed(tmp_path, monkeypatch)
    auto._record({**item, "analysis_job_id": job["id"]}, "manual_required", "freshness_or_quality_gate")
    before = hashlib.sha256(json.dumps(store.asset(item["id"]), sort_keys=True).encode()).hexdigest()
    assert auto.tick()["blocked_reasons"] == {"manual_review_required": 1}
    assert hashlib.sha256(json.dumps(store.asset(item["id"]), sort_keys=True).encode()).hexdigest() == before
    assert analysis.run_pending() is None and provider.calls == 1
