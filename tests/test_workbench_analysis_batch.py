"""Batch analysis uses synthetic images and in-process providers, never paid APIs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import threading
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from omnisignal.workbench.analysis import AnalysisService
from omnisignal.workbench.app import create_app
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.store import Conflict, Store


BASE = "/workbench/api"
SUGGESTION = {"purposes": ["brand"], "kind": "advertisement", "reason": "合成测试图中的颜色块观察",
              "description": "合成样本", "ocr_text": "", "brand": ""}


class FakeProvider:
    cache_identity = "batch-synthetic-provider"
    model_version = "synthetic-model"

    def __init__(self, failing=False):
        self.calls = 0
        self.failing = failing

    def analyze(self, asset, image):
        self.calls += 1
        if self.failing:
            raise RuntimeError("private-provider-response-must-not-leak")
        return deepcopy(SUGGESTION)


def selected(*assets):
    return [{"asset_id": asset["id"], "expected_revision": asset["revision"]} for asset in assets]


def add_asset(store, number, permission="allowed", status="pending"):
    picture = io.BytesIO()
    Image.new("RGB", (40, 40), (number % 255, number // 255, 90)).save(picture, "PNG")
    asset = store.import_asset({"title": f"合成样本 {number}", "source_url": f"https://example.invalid/{number}"}, picture.getvalue())
    patch = {"expected_revision": asset["revision"]}
    if permission != "unknown":
        patch.update(permissions={"display": "unknown", "analysis": permission, "export": "unknown"},
                     permission_note="本地自建合成测试素材")
    if status != "pending":
        patch.update(status=status, reason="合成样本状态测试")
    return store.review(asset["id"], patch, "admin", "admin") if len(patch) > 1 else asset


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "3")
    store = Store(tmp_path / "workbench")
    provider = FakeProvider()
    service = AnalysisService(store, provider)
    return store, service, provider


def test_preview_matches_pending_screen_without_writes_or_provider_calls(prepared):
    store, service, provider = prepared
    allowed = [add_asset(store, number) for number in range(2)]
    for asset in allowed:
        service.enqueue(asset["id"], asset["revision"], "admin")
        service.run_pending()
    unknown = [add_asset(store, number, "unknown") for number in range(2, 34)]
    with store.db() as db:
        before = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                  for name in ("audit", "analysis_jobs", "analysis_batches")}
    response = service.preview_batch(selected(*allowed, *unknown))
    assert response["state"] == "preview" and response["batch_id"] is None
    assert response["totals"]["eligible"] == 0 and response["totals"]["skipped"] == 34
    assert [item["reason_code"] for item in response["items"][:2]] == ["already_analyzed"] * 2
    assert {item["reason_code"] for item in response["items"][2:]} == {"permission_required"}
    with store.db() as db:
        after = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in before}
    assert after == before and provider.calls == 2
    assert all(store.asset(asset["id"])["permissions"]["analysis"] == "unknown" for asset in unknown)


def test_partial_batch_idempotency_live_progress_and_no_auto_approval(prepared):
    store, service, provider = prepared
    assets = [add_asset(store, index) for index in range(4)]
    assets.append(add_asset(store, 5, "unknown"))
    payload, request_id = selected(*assets), str(uuid.uuid4())
    preview = service.preview_batch(payload)
    assert preview["totals"]["eligible"] == 3
    assert preview["items"][3]["reason_code"] == "budget_exhausted"
    batch = service.enqueue_batch(request_id, payload, "admin")
    assert batch["batch_id"] == request_id.replace("-", "")
    assert batch["totals"] == {"requested": 5, "eligible": 0, "submitted": 3, "skipped": 2,
                               "queued": 3, "running": 0, "completed": 0, "failed": 0}
    assert batch["quota"]["used"] == 0 and batch["quota"]["reserved"] == 3 and batch["quota"]["remaining"] == 0
    repeated = service.enqueue_batch(request_id, payload, "admin")
    assert repeated == batch and len(service.jobs()) == 3 and provider.calls == 0
    while service.run_pending():
        pass
    done = service.batch(batch["batch_id"], "admin")
    assert done["state"] == "finished" and done["totals"]["completed"] == 3
    assert done["quota"]["used"] == 3 and done["quota"]["reserved"] == 0
    assert all(item["charged"] for item in done["items"][:3])
    assert provider.calls == 3
    for asset in assets:
        current = store.asset(asset["id"])
        assert current["status"] == "pending" and current["revision"] == asset["revision"]
        assert current["permissions"] == asset["permissions"] and current["description"] == asset["description"]
    assert service.enqueue_batch(request_id, payload, "admin") == done


def test_idempotency_actor_and_payload_are_bound_and_batch_reads_are_isolated(prepared):
    store, service, _ = prepared
    first, second = add_asset(store, 1), add_asset(store, 2)
    request_id = str(uuid.uuid4())
    batch = service.enqueue_batch(request_id, selected(first), "alice")
    for actor, payload in (("bob", selected(first)), ("alice", selected(second))):
        with pytest.raises(Conflict, match="标识已使用"):
            service.enqueue_batch(request_id, payload, actor)
    assert service.batch(batch["batch_id"], "bob") is None
    assert service.batch(batch["batch_id"], "alice")["batch_id"] == batch["batch_id"]
    assert len(service.jobs()) == 1


def test_submit_revalidates_revision_permissions_and_status_after_preview(prepared):
    store, service, provider = prepared
    assets = [add_asset(store, index) for index in range(3)]
    assert service.preview_batch(selected(*assets))["totals"]["eligible"] == 3
    store.review(assets[0]["id"], {"expected_revision": assets[0]["revision"], "description": "编辑刚更新"}, "editor", "operator")
    denied = store.review(assets[1]["id"], {"expected_revision": assets[1]["revision"], "permissions": {"display": "unknown", "analysis": "denied", "export": "unknown"}, "permission_note": "撤回模型分析用途"}, "admin", "admin")
    excluded = store.review(assets[2]["id"], {"expected_revision": assets[2]["revision"], "status": "excluded", "reason": "排除测试"}, "admin", "admin")
    batch = service.enqueue_batch(str(uuid.uuid4()), selected(assets[0], denied, excluded), "admin")
    assert [item["reason_code"] for item in batch["items"]] == ["revision_changed", "permission_required", "not_pending"]
    assert batch["totals"]["submitted"] == 0 and batch["state"] == "finished"
    assert not service.jobs() and provider.calls == 0


def test_queued_batch_rechecks_edits_before_any_model_call(prepared):
    store, service, provider = prepared
    asset = add_asset(store, 1)
    batch = service.enqueue_batch(str(uuid.uuid4()), selected(asset), "admin")
    store.review(asset["id"], {"expected_revision": asset["revision"], "description": "入队后更新"}, "editor", "operator")
    assert service.run_pending()["status"] == "stale"
    done = service.batch(batch["batch_id"], "admin")
    assert done["totals"]["failed"] == 1 and not done["items"][0]["charged"]
    assert done["quota"]["remaining"] == 3 and provider.calls == 0


def test_failed_batch_is_never_automatically_retried(prepared):
    store, service, provider = prepared
    provider.failing = True
    asset = add_asset(store, 1)
    first = service.enqueue_batch(str(uuid.uuid4()), selected(asset), "admin")
    assert service.run_pending()["status"] == "failed"
    assert service.run_pending() is None
    second = service.enqueue_batch(str(uuid.uuid4()), selected(asset), "admin")
    assert second["items"][0]["reason_code"] == "retry_required" and not second["totals"]["submitted"]
    assert service.batch(first["batch_id"], "admin")["totals"]["failed"] == 1
    assert "private-provider-response" not in json.dumps(second)
    assert provider.calls == 1 and service.configuration()["used"] == 1
    service.enqueue(asset["id"], asset["revision"], "admin", retry=True)
    service.run_pending()
    assert provider.calls == 2


def test_new_batch_skips_existing_inflight_job_without_new_reservation(prepared):
    store, service, _ = prepared
    asset = add_asset(store, 1)
    first = service.enqueue_batch(str(uuid.uuid4()), selected(asset), "admin")
    second = service.enqueue_batch(str(uuid.uuid4()), selected(asset), "admin")
    assert second["items"][0]["reason_code"] == "already_running"
    assert second["totals"]["submitted"] == 0 and second["quota"]["reserved"] == 1
    assert len(service.jobs()) == 1 and first["totals"]["submitted"] == 1


def test_concurrent_batches_cannot_overbook_daily_capacity(prepared):
    store, service, _ = prepared
    assets = [add_asset(store, number) for number in range(6)]
    gate = threading.Barrier(2)
    def submit(group):
        gate.wait(timeout=5)
        return service.enqueue_batch(str(uuid.uuid4()), selected(*group), "admin")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, assets[:3]), pool.submit(submit, assets[3:])]
        batches = [future.result(timeout=10) for future in futures]
    assert sum(batch["totals"]["submitted"] for batch in batches) == 3
    assert len(service.jobs()) == 3 and service.configuration()["reserved"] == 3
    assert service.configuration()["used"] == 0


def test_concurrent_duplicate_batch_has_one_durable_result(prepared):
    store, service, _ = prepared
    asset = add_asset(store, 1)
    request_id, gate = str(uuid.uuid4()), threading.Barrier(2)
    def submit():
        gate.wait(timeout=5)
        return service.enqueue_batch(request_id, selected(asset), "admin")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        batches = [future.result(timeout=10) for future in futures]
    assert batches[0] == batches[1] and len(service.jobs()) == 1
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM analysis_batches").fetchone()[0] == 1


def test_single_image_queue_and_batch_share_admission_capacity(prepared, monkeypatch):
    store, service, _ = prepared
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "1")
    first, second = add_asset(store, 1), add_asset(store, 2)
    service.enqueue(first["id"], first["revision"], "admin")
    assert service.preview_batch(selected(second))["items"][0]["reason_code"] == "budget_exhausted"
    batch = service.enqueue_batch(str(uuid.uuid4()), selected(second), "admin")
    assert batch["totals"]["submitted"] == 0
    with pytest.raises(Conflict, match="已分配"):
        service.enqueue(second["id"], second["revision"], "admin")


def test_queue_capacity_is_separate_from_daily_limit(prepared, monkeypatch):
    store, service, _ = prepared
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "1000")
    asset = add_asset(store, 1)
    with store.db() as db:
        for index in range(100):
            db.execute("INSERT INTO analysis_jobs(id,asset_id,revision,cache_key,provider,actor,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (str(index), "historical-asset", 1, str(index), "test", "admin", "queued", "2026-01-01T00:00:00+00:00"))
    preview = service.preview_batch(selected(asset))
    assert preview["quota"]["remaining"] == 900 and preview["quota"]["queue_remaining"] == 0
    assert preview["items"][0]["reason_code"] == "queue_full"


def test_utc_day_rollover_reserves_old_queued_but_not_charged_remote_reconciliation(prepared, monkeypatch):
    store, service, _ = prepared
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "2")
    monkeypatch.setattr(store, "day", lambda: "2026-10-26")
    assets = [add_asset(store, number) for number in range(3)]
    service.enqueue_batch(str(uuid.uuid4()), selected(*assets[:2]), "admin")
    with store.db() as db:
        job = db.execute("SELECT id FROM analysis_jobs LIMIT 1").fetchone()[0]
        db.execute("UPDATE analysis_jobs SET status='waiting_remote',charged=1,quota_day='2026-10-26',remote_request_id=? WHERE id=?", (str(uuid.uuid4()), job))
        db.execute("INSERT INTO analysis_usage VALUES('2026-10-26',1)")
    monkeypatch.setattr(store, "day", lambda: "2026-10-27")
    quota = service.preview_batch(selected(assets[2]))["quota"]
    assert quota["used"] == 0 and quota["reserved"] == 1 and quota["remaining"] == 1


def test_restart_preserves_batch_identity_and_job_links(prepared):
    store, service, _ = prepared
    asset = add_asset(store, 1)
    request_id = str(uuid.uuid4())
    original = service.enqueue_batch(request_id, selected(asset), "admin")
    restarted_provider = FakeProvider()
    restarted = AnalysisService(Store(store.root), restarted_provider)
    assert restarted.batch(original["batch_id"], "admin") == original
    assert restarted.enqueue_batch(request_id, selected(asset), "admin") == original
    restarted.run_pending()
    assert restarted_provider.calls == 1
    assert restarted.batch(original["batch_id"], "admin")["state"] == "finished"


def test_batch_insert_rolls_back_all_jobs_if_audit_write_fails(prepared, monkeypatch):
    store, service, _ = prepared
    assets = [add_asset(store, index) for index in range(2)]
    real_audit = store.audit
    def audit(db, entity, actor, action, details=None):
        if action == "analysis_batch_enqueue":
            raise RuntimeError("synthetic storage failure")
        return real_audit(db, entity, actor, action, details)
    monkeypatch.setattr(store, "audit", audit)
    with pytest.raises(RuntimeError, match="storage failure"):
        service.enqueue_batch(str(uuid.uuid4()), selected(*assets), "admin")
    assert not service.jobs() and service.configuration()["reserved"] == 0
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM analysis_batches").fetchone()[0] == 0
    assert all(store.asset(asset["id"])["analysis_status"] == "not_analyzed" for asset in assets)


@pytest.mark.parametrize("items", [[], None, {}, [None], [{"asset_id": "a", "expected_revision": True}],
    [{"asset_id": "a", "expected_revision": 0}], [{"asset_id": "a", "expected_revision": 1, "retry": True}],
    [{"asset_id": "a", "expected_revision": 1}] * 2,
    [{"asset_id": str(i), "expected_revision": 1} for i in range(101)]])
def test_invalid_batch_selection_is_rejected_before_writes(prepared, items):
    _, service, provider = prepared
    with pytest.raises(Conflict):
        service.preview_batch(items)
    with pytest.raises(Conflict):
        service.enqueue_batch(str(uuid.uuid4()), items, "admin")
    assert not service.jobs() and provider.calls == 0


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_USERS_FILE", raising=False)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "3")
    provider = FakeProvider()
    import omnisignal.workbench.analysis as module
    monkeypatch.setattr(module, "AnalysisService", lambda store: AnalysisService(store, provider))
    app = create_app(tmp_path / "api-store", start_worker=False)
    hashed = password_hash("synthetic-test-password-only")
    (app.state.store.root / "users.json").write_text(json.dumps([
        {"username": role, "role": role, "password_hash": hashed} for role in ("admin", "operator", "viewer")]), encoding="utf-8")
    def login(role):
        client = TestClient(app)
        response = client.post(BASE + "/session", json={"username": role, "password": "synthetic-test-password-only"})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client
    return app, login, provider


def test_api_auth_csrf_actor_isolation_and_worker_guard(api):
    app, login, provider = api
    asset = add_asset(app.state.store, 1)
    payload = {"items": selected(asset)}
    anonymous = TestClient(app)
    assert anonymous.post(BASE + "/analysis/batches/preview", json=payload).status_code == 401
    viewer = login("viewer")
    assert viewer.post(BASE + "/analysis/batches/preview", json=payload).status_code == 403
    assert viewer.get(BASE + "/analysis/batches/" + uuid.uuid4().hex).status_code == 403
    editor = login("operator")
    assert editor.post(BASE + "/analysis/batches/preview", json=payload, headers={"X-CSRF-Token": ""}).status_code == 403
    assert editor.post(BASE + "/analysis/batches/preview", json=payload, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert editor.post(BASE + "/analysis/batches/preview", json=payload).json()["totals"]["eligible"] == 1
    payload["request_id"] = str(uuid.uuid4())
    assert editor.post(BASE + "/analysis/batches", json=payload).status_code == 409
    # A synthetic worker-presence marker permits durable enqueue without executing a model.
    app.state.collector = object()
    response = editor.post(BASE + "/analysis/batches", json=payload)
    assert response.status_code == 202
    batch_id = response.json()["batch_id"]
    assert editor.get(BASE + "/analysis/batches/" + batch_id).status_code == 200
    other_actor = login("admin")
    assert other_actor.get(BASE + "/analysis/batches/" + batch_id).status_code == 404
    assert other_actor.post(BASE + "/analysis/batches", json=payload).status_code == 409
    assert editor.post(BASE + "/analysis/batches", json=payload).json()["items"] == response.json()["items"]
    assert provider.calls == 0


def test_api_rejects_retry_and_malformed_id_instead_of_silently_charging(api):
    app, login, _ = api
    editor = login("operator")
    asset = add_asset(app.state.store, 1)
    for payload in ({"items": selected(asset), "retry": True}, {"items": selected(asset) * 2}, {"items": []}):
        assert editor.post(BASE + "/analysis/batches/preview", json=payload).status_code == 422
    assert editor.post(BASE + "/analysis/batches", json={"items": selected(asset), "request_id": "invalid"}).status_code == 422
    assert editor.get(BASE + "/analysis/batches/invalid").status_code == 422
    assert editor.get(BASE + "/analysis/batches/" + uuid.uuid4().hex).status_code == 404
