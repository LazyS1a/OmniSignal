"""Real isolated SQLite/file races, with synthetic images and zero paid calls."""
from copy import deepcopy
import hashlib
import io
import json
import sqlite3
import threading

from PIL import Image
import pytest

from omnisignal.knowledge.release import KnowledgeError, _lock
from omnisignal.workbench.analysis import AnalysisService, _result
from omnisignal.workbench.analysis_recovery import main, recover_analysis_projections
from omnisignal.workbench.auto_editorial import AutoEditorial
from omnisignal.workbench.review import completed_source_job
from omnisignal.workbench.store import Conflict, Store, encode, stamp
from test_workbench_auto_editorial import CONFIG, OfflineProvider, SUGGESTION


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "100")
    monkeypatch.delenv("OMNISIGNAL_KNOWLEDGE_DIR", raising=False)
    store = Store(tmp_path / "store")
    raw = io.BytesIO()
    Image.new("RGB", (96, 64), "blue").save(raw, "PNG")
    item = store.import_asset({"title": "合成测试图", "source_id": "potw",
                               "source_url": "https://example.invalid/test", "description": "未修改的合成测试说明"}, raw.getvalue())
    item = store.review(item["id"], {"expected_revision": 1, "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"},
                                   "permission_note": "仅合成流程测试"}, "synthetic-admin", "admin")
    provider = OfflineProvider()
    service = AnalysisService(store, provider)
    return store, item, service, provider


def mutate(store, asset_id, change, *, row_delta=0):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        asset = json.loads(row["document"])
        change(asset)
        db.execute("UPDATE assets SET document=?,revision=revision+? WHERE id=?", (encode(asset), row_delta, asset_id))


def test_row_document_drift_blocks_single_and_batch_admission_without_charge(prepared):
    store, item, service, provider = prepared
    mutate(store, item["id"], lambda asset: None, row_delta=1)
    shown = store.asset(item["id"])
    assert shown["revision"] == item["revision"] + 1
    assert shown["storage_consistency"] == "revision_mismatch"
    assert store.list_assets(status="all")["items"][0]["revision"] == shown["revision"]
    with pytest.raises(Conflict, match="存储版本不一致"):
        service.enqueue(item["id"], shown["revision"], "synthetic")
    preview = service.preview_batch([{"asset_id": item["id"], "expected_revision": shown["revision"]}])
    assert preview["items"][0]["reason_code"] == "storage_inconsistent"
    assert service.jobs() == [] and provider.calls == service.configuration()["used"] == 0


@pytest.mark.parametrize("change", [
    lambda asset: asset.update(description="另一个任务的上下文"),
    lambda asset: asset.update(analysis_job_id="foreign-task"),
    lambda asset: asset.update(image_file="foreign.png"),
])
def test_same_revision_input_or_task_change_before_execution_is_not_charged(prepared, change):
    store, item, service, provider = prepared
    service.enqueue(item["id"], item["revision"], "synthetic")
    mutate(store, item["id"], change)
    assert service.run_pending()["status"] == "stale"
    assert provider.calls == service.configuration()["used"] == 0


@pytest.mark.parametrize("during", ["row_drift", "metadata", "task", "image_bytes", "cas_ignored"])
def test_paid_request_completion_never_reports_an_unapplied_projection_as_success(prepared, during):
    store, item, service, provider = prepared
    entered, release = threading.Event(), threading.Event()
    original = provider.analyze
    def analyze(asset, image):
        entered.set()
        assert release.wait(5)
        return original(asset, image)
    provider.analyze = analyze
    job = service.enqueue(item["id"], item["revision"], "synthetic")
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(service.run_pending()))
    worker.start()
    assert entered.wait(5)
    try:
        if during == "row_drift":
            mutate(store, item["id"], lambda asset: None, row_delta=1)
        elif during == "metadata":
            mutate(store, item["id"], lambda asset: asset.update(description="并发人工变更"))
        elif during == "task":
            mutate(store, item["id"], lambda asset: asset.update(analysis_job_id="later-task"))
        elif during == "image_bytes":
            (store.images / item["image_file"]).write_bytes(b"changed-during-paid-request")
        else:
            with store.db() as db:
                db.execute("CREATE TRIGGER ignore_projection BEFORE UPDATE ON assets BEGIN SELECT RAISE(IGNORE); END")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes[0]["status"] == "stale" and outcomes[0]["charged"]
    assert provider.calls == service.configuration()["used"] == 1
    stored = store.asset(item["id"])
    assert stored.get("analysis_suggestion") is None and stored["analysis_suggestion_job_id"] is None
    if during in {"row_drift", "cas_ignored"}:
        assert outcomes[0]["suggestion"]["source_image_id"] == item["id"]  # retain paid evidence
    with store.db() as db:
        assert not db.execute("SELECT 1 FROM audit WHERE action='analysis_completed' AND entity=?", (item["id"],)).fetchone()
        assert completed_source_job(db, {**item, "analysis_job_id": job["id"]}, job["id"]) is None


def legacy_case(prepared, tmp_path, status="completed"):
    store, item, service, provider = prepared
    # Reproduce the exact legacy corruption before the frozen deployment backup.
    mutate(store, item["id"], lambda asset: None, row_delta=1)
    baseline = tmp_path / "frozen-before-jobs.sqlite3"
    with store.db() as source, sqlite3.connect(baseline) as target:
        source.backup(target)
    job_id = "0123456789abcdef0123456789abcdef"
    suggestion = {**_result(deepcopy(SUGGESTION), item["id"], require_taxonomy=True), "model_version": provider.model_version}
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        asset = json.loads(db.execute("SELECT document FROM assets WHERE id=?", (item["id"],)).fetchone()[0])
        asset.update(analysis_status="analyzing", analysis_job_id=job_id)
        db.execute("UPDATE assets SET document=? WHERE id=?", (encode(asset), item["id"]))
        cache = hashlib.sha256(encode([item["id"], item["revision"], item["sha256"], provider.cache_identity]).encode()).hexdigest()
        db.execute("INSERT INTO analysis_jobs(id,asset_id,revision,cache_key,provider,actor,status,created_at,charged,suggestion) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (job_id, item["id"], item["revision"], cache, provider.cache_identity, "synthetic", status, stamp(), int(status != "queued"), encode(suggestion) if status == "completed" else None))
        store.audit(db, item["id"], "synthetic", "analysis_enqueue", {"job_id": job_id, "revision": item["revision"]})
        if status == "completed":
            store.audit(db, item["id"], "synthetic", "analysis_completed", {"job_id": job_id, "revision": item["revision"]})
            db.execute("INSERT INTO analysis_usage VALUES(?,1)", (store.day(),))
    return baseline, job_id


def test_paid_legacy_result_recovers_once_and_auto_review_uses_it_without_publish(prepared, tmp_path):
    store, item, service, provider = prepared
    baseline, job_id = legacy_case(prepared, tmp_path)
    with store.db() as db:
        original = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone())
    plan = recover_analysis_projections(store, baseline, [item["id"]])
    assert plan["items"][0]["status"] == "eligible" and not plan["applied"]
    assert "analysis_suggestion" not in store.asset(item["id"])
    result = recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])
    assert result["applied"] and result["recovered"] == 1 and result["provider_calls"] == 0
    restored = store.asset(item["id"])
    assert restored["analysis_suggestion_revision"] == restored["revision"] == item["revision"] + 1
    assert restored["analysis_suggestion_job_id"] == job_id and restored["analysis_status"] == "analyzed"
    replay = recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])
    assert replay["already_recovered"] and not replay["applied"]
    assert service.enqueue(item["id"], restored["revision"], "synthetic")["id"] == job_id
    assert service.run_pending() is None and provider.calls == 0 and service.configuration()["used"] == 1
    with store.db() as db:
        after = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone())
        assert all(after[key] == value for key, value in original.items() if key not in {"input_binding", "asset_revision", "projection_evidence"})
        assert db.execute("SELECT count(*) FROM audit WHERE action='analysis_projection_recovered'").fetchone()[0] == 1
    auto = AutoEditorial(store, service, config={**CONFIG, "auto_publish": False})
    reviewed = auto.tick()
    assert reviewed["reviewed"] == 1 and reviewed["published"] == 0
    current = store.asset(item["id"])
    assert current["reviewed_analysis"]["provenance"] == "model_automated_review"
    assert current["permissions"] == item["permissions"]
    assert provider.calls == 0 and service.configuration()["used"] == 1


def test_legacy_queued_job_keeps_identity_and_runs_one_previously_authorized_call(prepared, tmp_path):
    store, item, service, provider = prepared
    baseline, job_id = legacy_case(prepared, tmp_path, "queued")
    plan = recover_analysis_projections(store, baseline, [item["id"]])
    recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])
    assert provider.calls == service.configuration()["used"] == 0
    result = service.run_pending()
    assert result["id"] == job_id and result["status"] == "completed"
    assert result["revision"] == item["revision"] and result["asset_revision"] == item["revision"] + 1
    assert provider.calls == service.configuration()["used"] == 1
    assert service.run_pending() is None


@pytest.mark.parametrize("corrupt", ["metadata", "permissions", "image_bytes", "job_cache", "job_identity", "review", "audit", "failed", "running", "result_source"])
def test_recovery_refuses_unproven_or_changed_evidence(prepared, tmp_path, corrupt):
    store, item, service, provider = prepared
    baseline, job_id = legacy_case(prepared, tmp_path)
    if corrupt == "metadata":
        mutate(store, item["id"], lambda asset: asset.update(description="内容已经更改"))
    elif corrupt == "permissions":
        mutate(store, item["id"], lambda asset: asset["permissions"].update(analysis="denied"))
    elif corrupt == "image_bytes":
        (store.images / item["image_file"]).write_bytes(b"tampered")
    elif corrupt == "review":
        mutate(store, item["id"], lambda asset: asset.update(reviewed_analysis={"human": True}))
    else:
        with store.db() as db:
            if corrupt == "job_cache":
                db.execute("UPDATE analysis_jobs SET cache_key='wrong' WHERE id=?", (job_id,))
            elif corrupt == "job_identity":
                db.execute("UPDATE analysis_jobs SET asset_id='foreign-asset' WHERE id=?", (job_id,))
            elif corrupt == "audit":
                store.audit(db, item["id"], "human", "review", {"revision": item["revision"]})
            elif corrupt in {"failed", "running"}:
                db.execute("UPDATE analysis_jobs SET status=? WHERE id=?", (corrupt, job_id))
            else:
                db.execute("UPDATE analysis_jobs SET suggestion=json_set(suggestion,'$.source_image_id','foreign-asset') WHERE id=?", (job_id,))
    plan = recover_analysis_projections(store, baseline, [item["id"]])
    assert plan["items"][0]["status"] == "blocked"
    with pytest.raises(Conflict, match="blocked_items"):
        recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])
    assert provider.calls == 0 and service.configuration()["used"] == 1


def test_recovery_rechecks_plan_and_rolls_back_on_audit_failure(prepared, tmp_path, monkeypatch):
    store, item, service, provider = prepared
    baseline, job_id = legacy_case(prepared, tmp_path)
    plan = recover_analysis_projections(store, baseline, [item["id"]])
    with pytest.raises(Conflict, match="plan_changed"):
        recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256="0" * 64)
    original_audit = store.audit
    def crash(db, entity, actor, action, details=None):
        if action == "analysis_projection_recovered":
            raise RuntimeError("synthetic crash after projection writes")
        original_audit(db, entity, actor, action, details)
    monkeypatch.setattr(store, "audit", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])
    with store.db() as db:
        row = db.execute("SELECT * FROM assets WHERE id=?", (item["id"],)).fetchone()
        job = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        assert row["revision"] == json.loads(row["document"])["revision"] + 1
        assert job["asset_revision"] is None and job["projection_evidence"] is None
    monkeypatch.setattr(store, "audit", original_audit)
    assert recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])["applied"]
    assert provider.calls == 0


def test_recovery_cannot_overlap_a_live_paid_worker(prepared, tmp_path):
    store, item, _, provider = prepared
    baseline, _ = legacy_case(prepared, tmp_path)
    plan = recover_analysis_projections(store, baseline, [item["id"]])
    with _lock(store.root / ".analysis-worker"):
        with pytest.raises(KnowledgeError):
            recover_analysis_projections(store, baseline, [item["id"]], apply=True, expected_plan_sha256=plan["plan_sha256"])
    assert provider.calls == 0 and "analysis_suggestion" not in store.asset(item["id"])


def test_maintenance_cli_defaults_to_read_only_and_needs_exact_plan_for_apply(prepared, tmp_path, capsys):
    store, item, _, provider = prepared
    baseline, _ = legacy_case(prepared, tmp_path)
    selection = tmp_path / "explicit-ids.json"
    selection.write_text(json.dumps([item["id"]]), encoding="utf-8")
    args = ["--root", str(store.root), "--baseline", str(baseline), "--asset-ids-file", str(selection)]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["applied"] is False and plan["items"][0]["status"] == "eligible"
    assert "analysis_suggestion" not in store.asset(item["id"])
    assert main(args + ["--apply", "--expected-plan-sha256", plan["plan_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["recovered"] == 1 and result["provider_calls"] == provider.calls == 0


def test_old_schema_dry_run_plan_survives_atomic_nullable_column_migration(prepared, tmp_path, capsys):
    store, item, _, provider = prepared
    baseline, job_id = legacy_case(prepared, tmp_path)
    with store.db() as db:
        for name in ("input_binding", "asset_revision", "projection_evidence"):
            db.execute(f"ALTER TABLE analysis_jobs DROP COLUMN {name}")
    selection = tmp_path / "legacy-ids.json"
    selection.write_text(json.dumps([item["id"]]), encoding="utf-8")
    args = ["--root", str(store.root), "--baseline", str(baseline), "--asset-ids-file", str(selection)]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    with store.db() as db:
        assert "input_binding" not in {row[1] for row in db.execute("PRAGMA table_info(analysis_jobs)")}
    assert main(args + ["--apply", "--expected-plan-sha256", plan["plan_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["plan_sha256"] == plan["plan_sha256"] and result["recovered"] == 1
    with store.db() as db:
        row = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
        assert row["input_binding"] and row["asset_revision"] == item["revision"] + 1
    assert provider.calls == 0
