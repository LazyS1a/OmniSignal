"""Real SQLite batch service + synthetic images; no network or paid model."""
from copy import deepcopy
from datetime import datetime
import io
import json
import threading

from PIL import Image
import pytest

from omnisignal.workbench.analysis import AnalysisService
from omnisignal.workbench.daily_pilot import ACTOR, DailyPilot, SHANGHAI, _hash, _request
from omnisignal.workbench.store import Store


POLICY = {"start_date": "2026-10-27", "end_date": "2026-11-02", "timezone": "Asia/Shanghai",
          "weekdays": [0, 1, 2, 3, 4], "hour": 16, "minute": 0, "target": 30,
          "review_target_min": 10, "review_target_max": 20}


def when(day="2026-10-27", hour=16, minute=0):
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=SHANGHAI)


class FakeProvider:
    cache_identity = "daily-pilot-synthetic-v1"
    model_version = "synthetic"

    def __init__(self):
        self.calls, self.fail = 0, False

    def analyze(self, asset, content):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private-provider-detail")
        return {"purposes": ["brand"], "kind": "advertisement", "reason": "合成图形观察",
                "description": "合成色块", "ocr_text": "", "brand": ""}


def add(store, number, permission="allowed", project=None):
    image = io.BytesIO()
    Image.new("RGB", (40, 40), (number % 256, number // 256, 30)).save(image, "PNG")
    asset = store.import_asset({"title": f"合成素材{number}", "source_url": f"https://example.invalid/{number}",
                                "project_id": project or f"project-{number}"}, image.getvalue())
    if permission != "unknown":
        asset = store.review(asset["id"], {"expected_revision": asset["revision"],
                             "permissions": {"display": "unknown", "analysis": permission, "export": "unknown"},
                             "permission_note": "合成测试素材"}, "tester", "admin")
    return asset


def selected(asset):
    return [{"asset_id": asset["id"], "expected_revision": asset["revision"]}]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "unlimited")
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_DAILY_PILOT_CONFIG", raising=False)
    store = Store(tmp_path / "store", clock=lambda: when().timestamp())
    provider = FakeProvider()
    analysis = AnalysisService(store, provider)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(POLICY), encoding="utf-8")
    return store, analysis, provider, path


def pilot(prepared):
    store, analysis, _, path = prepared
    return DailyPilot(store, analysis, policy_path=path)


def intent(p, day="2026-10-27"):
    path = p.root / f"intent-{day}.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def jobs(analysis):
    with analysis.store.db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM analysis_jobs ORDER BY rowid")]


def drain(analysis):
    while analysis.run_pending() is not None:
        pass


def test_disabled_by_default_and_snapshot_cannot_schedule(prepared, monkeypatch):
    store, analysis, provider, _ = prepared
    add(store, 1)
    disabled = DailyPilot(store, analysis)
    disabled.start()
    assert disabled.tick(when())["state"] == "disabled"
    assert not disabled.root.exists() and disabled._thread is None
    p = pilot(prepared)
    monkeypatch.setattr(analysis, "preview_batch", lambda *args: pytest.fail("snapshot must not preview"))
    monkeypatch.setattr(analysis, "enqueue_batch", lambda *args: pytest.fail("snapshot must not submit"))
    snapshot = p.snapshot()
    snapshot["enabled"] = False
    assert p.snapshot()["enabled"] is True
    assert p.snapshot()["state"] == "not_started" and not jobs(analysis) and provider.calls == 0


def test_start_stop_is_idempotent_and_wait_is_interruptible(prepared, monkeypatch):
    p = pilot(prepared)
    entered = threading.Event()
    calls = []
    def tick():
        calls.append(True)
        entered.set()
    monkeypatch.setattr(p, "tick", tick)
    p.start()
    assert entered.wait(2)
    original_thread = p._thread
    p.start()
    assert p._thread is original_thread
    p.stop()
    p.stop()
    assert not original_thread.is_alive() and len(calls) == 1


@pytest.mark.parametrize("day,hour,minute,state", [
    ("2026-10-26", 20, 0, "before_period"), ("2026-10-27", 15, 59, "waiting_window"),
    ("2026-10-31", 16, 0, "weekend"), ("2026-11-01", 16, 0, "weekend"),
    ("2026-11-03", 16, 0, "ended"),
])
def test_window_never_backfills_or_automatically_expands(prepared, day, hour, minute, state):
    store, analysis, provider, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    result = p.tick(when(day, hour, minute))
    assert result["state"] == state and not jobs(analysis) and provider.calls == 0
    assert not list(p.root.glob("intent-*.json"))


@pytest.mark.parametrize("patch", [
    {"start_date": "2026-10-26"}, {"end_date": "2026-11-03"}, {"timezone": "UTC"},
    {"weekdays": [0, 1, 2, 3, 4, 5]}, {"weekdays": [False, 1, 2, 3, 4]},
    {"hour": 15}, {"minute": 1}, {"target": 0}, {"target": 101}, {"target": True},
    {"review_target_min": 9}, {"review_target_max": 21}, {"model": "unsafe-override"},
    {"permissions": "allowed"}, {"publish": True},
])
def test_invalid_policy_fails_closed(prepared, patch):
    store, analysis, provider, path = prepared
    add(store, 1)
    path.write_text(json.dumps({**POLICY, **patch}), encoding="utf-8")
    result = pilot(prepared).tick(when())
    assert result["state"] == "blocked" and result["diagnostic"] == "invalid_policy"
    assert not jobs(analysis) and provider.calls == 0


@pytest.mark.parametrize("contents", ["{", "[]", '{"target":30,"target":31}', "x" * 16001])
def test_malformed_policy_never_logs_payload(prepared, contents):
    _, analysis, _, path = prepared
    path.write_text(contents, encoding="utf-8")
    result = pilot(prepared).tick(when())
    assert result["diagnostic"] == "invalid_policy" and not jobs(analysis)
    assert "private-provider-detail" not in json.dumps(result)


@pytest.mark.parametrize("amount,expected", [(0, 0), (29, 29), (30, 30), (31, 30)])
def test_daily_target_boundaries_and_same_day_restart(prepared, amount, expected):
    store, analysis, provider, _ = prepared
    for number in range(amount):
        add(store, number)
    p = pilot(prepared)
    first = p.tick(when())
    assert first["report"]["submitted"] == expected
    assert first["report"]["submission_shortfall"] == 30 - expected
    assert first["report"]["analysed_images"] == 0 and provider.calls == 0
    _, saved = intent(p)
    assert saved["actor"] == ACTOR and saved["request_id"] == _request("2026-10-27")
    assert len(saved["items"]) == expected
    assert p.tick(when(hour=17))["report"]["submitted"] == expected
    assert pilot(prepared).tick(when(hour=18))["report"]["submitted"] == expected
    assert len(jobs(analysis)) == expected


def test_projects_round_robin_and_real_progress_remain_pending(prepared):
    store, analysis, provider, _ = prepared
    assets = [add(store, number, project="many-images") for number in range(35)]
    assets += [add(store, number, project=f"other-{number}") for number in range(35, 65)]
    before = {asset["id"]: (asset["status"], asset["permissions"]) for asset in assets}
    p = pilot(prepared)
    report = p.tick(when())["report"]
    assert report["submitted"] == 30 and report["submitted_projects"] == 30
    assert report["projects_covered"] == 0 and report["reviewed_count"] is None
    drain(analysis)
    report = p.tick(when(hour=17))["report"]
    assert report["analysed_images"] == 30 and report["projects_covered"] == 30
    assert report["requires_human_review"] == 30 and provider.calls == 30
    assert (report["review_target_min"], report["review_target_max"], report["reviewed_count"]) == (10, 20, None)
    for asset in assets:
        current = store.asset(asset["id"])
        assert (current["status"], current["permissions"]) == before[asset["id"]]
    with store.db() as db:
        actions = {row[0] for row in db.execute("SELECT action FROM audit WHERE actor=?", (ACTOR,))}
        assert not db.execute("SELECT 1 FROM releases").fetchone()
    assert actions == {"analysis_enqueue", "analysis_batch_enqueue", "analysis_completed"}


def test_mixed_permissions_analysis_history_and_no_failed_retry(prepared):
    store, analysis, provider, _ = prepared
    allowed, unknown, denied, done, failed, running = [add(store, n, permission) for n, permission in enumerate(
        ["allowed", "unknown", "denied", "allowed", "allowed", "allowed"])]
    analysis.enqueue(done["id"], done["revision"], "manual")
    analysis.run_pending()
    provider.fail = True
    analysis.enqueue(failed["id"], failed["revision"], "manual")
    analysis.run_pending()
    provider.fail = False
    analysis.enqueue(running["id"], running["revision"], "manual")
    p = pilot(prepared)
    report = p.tick(when())["report"]
    assert report["submitted"] == 1
    assert report["skip_reasons"] == {"permission_required": 2, "already_analyzed": 1,
                                      "retry_required": 1, "already_running": 1}
    assert [job["asset_id"] for job in jobs(analysis) if job["actor"] == ACTOR] == [allowed["id"]]
    drain(analysis)
    tomorrow = p.tick(when("2026-10-28"))["report"]
    assert tomorrow["submitted"] == 0 and tomorrow["skip_reasons"]["retry_required"] == 1
    assert store.asset(unknown["id"])["permissions"]["analysis"] == "unknown"
    assert store.asset(denied["id"])["permissions"]["analysis"] == "denied"
    assert len(jobs(analysis)) == 4 and provider.calls == 4


def test_zero_eligible_is_final_for_today_and_unlimited_manual_is_independent(prepared):
    store, analysis, _, _ = prepared
    add(store, 1, "unknown")
    p = pilot(prepared)
    assert p.tick(when())["report"]["state"] == "no_eligible"
    new = add(store, 2)
    assert p.tick(when(hour=17))["report"]["submitted"] == 0
    manual = analysis.enqueue(new["id"], new["revision"], "manual")
    assert manual["status"] == "queued" and analysis.configuration()["request_limit"] is None
    assert len(jobs(analysis)) == 1


class Crash(BaseException):
    pass


def test_crash_before_submit_replays_exact_persisted_payload(prepared, monkeypatch):
    store, analysis, provider, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    original = analysis.enqueue_batch
    payloads = []
    def crash(request, items, actor):
        payloads.append((request, deepcopy(items), actor))
        assert intent(p)[1]["items"] == items  # Durable before first enqueue.
        raise Crash()
    monkeypatch.setattr(analysis, "enqueue_batch", crash)
    with pytest.raises(Crash):
        p.tick(when())
    add(store, 2)  # Restart must not expand yesterday's saved selection.
    def resumed(request, items, actor):
        payloads.append((request, deepcopy(items), actor))
        return original(request, items, actor)
    monkeypatch.setattr(analysis, "enqueue_batch", resumed)
    assert pilot(prepared).tick(when(hour=17))["report"]["submitted"] == 1
    assert payloads[0] == payloads[1] and len(jobs(analysis)) == 1 and provider.calls == 0


def test_lost_submit_response_queries_original_without_second_submission(prepared, monkeypatch):
    store, analysis, _, _ = prepared
    add(store, 1)
    original, calls = analysis.enqueue_batch, []
    def lost(*args):
        calls.append(args)
        original(*args)
        raise RuntimeError("private-provider-detail")
    monkeypatch.setattr(analysis, "enqueue_batch", lost)
    p = pilot(prepared)
    assert p.tick(when())["report"]["submitted"] == 1
    assert pilot(prepared).tick(when(hour=17))["report"]["submitted"] == 1
    assert len(calls) == 1 and len(jobs(analysis)) == 1


def test_postcommit_state_write_crash_recovers_existing_batch(prepared, monkeypatch):
    store, analysis, _, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    original = p._save
    def fail_observed(state):
        if state["phase"] == "observed":
            raise OSError("private-filesystem-path")
        original(state)
    monkeypatch.setattr(p, "_save", fail_observed)
    assert p.tick(when())["diagnostic"] == "pilot_operation_failed"
    assert intent(p)[1]["phase"] == "prepared" and len(jobs(analysis)) == 1
    assert pilot(prepared).tick(when(hour=17))["report"]["submitted"] == 1
    assert len(jobs(analysis)) == 1


def test_crossday_unsubmitted_intent_expires_without_backfill(prepared, monkeypatch):
    store, analysis, _, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    original, requests = analysis.enqueue_batch, []
    monkeypatch.setattr(analysis, "enqueue_batch", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        p.tick(when())
    def record(request, items, actor):
        requests.append(request)
        return original(request, items, actor)
    monkeypatch.setattr(analysis, "enqueue_batch", record)
    result = pilot(prepared).tick(when("2026-10-28"))
    assert requests == [_request("2026-10-28")]
    old = next(r for r in result["recent"] if r["day"] == "2026-10-27")
    assert old["state"] == "expired_intent" and old["submitted"] == 0
    assert intent(p)[1]["phase"] == "expired" and len(jobs(analysis)) == 1


def test_after_window_only_existing_batch_is_refreshed(prepared):
    store, analysis, provider, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    assert p.tick(when("2026-11-02"))["report"]["submitted"] == 1
    drain(analysis)
    add(store, 2)
    result = pilot(prepared).tick(when("2026-11-03"))
    assert result["state"] == "ended" and result["report"] is None
    assert result["recent"][0]["analysed_images"] == 1
    assert len(jobs(analysis)) == 1 and provider.calls == 1


@pytest.mark.parametrize("corruption", ["json", "checksum", "actor", "extra", "revision"])
def test_corrupt_state_blocks_all_new_work(prepared, corruption):
    store, analysis, _, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    p.tick(when())
    path, state = intent(p)
    if corruption == "json":
        path.write_text("{private-filesystem-path", encoding="utf-8")
    else:
        if corruption == "checksum": state["checksum"] = "invalid"
        if corruption == "actor": state["actor"] = "admin"
        if corruption == "extra": state["publish"] = True
        if corruption == "revision": state["items"][0]["expected_revision"] = True
        if corruption != "checksum": state["checksum"] = _hash({k: v for k, v in state.items() if k != "checksum"})
        path.write_text(json.dumps(state), encoding="utf-8")
    add(store, 2)
    result = pilot(prepared).tick(when("2026-10-28"))
    assert result["diagnostic"] == "invalid_state" and len(jobs(analysis)) == 1
    assert "private-filesystem-path" not in json.dumps(result)


def test_unknown_old_batch_read_blocks_today_and_missing_observed_fails_closed(prepared, monkeypatch):
    store, analysis, _, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    p.tick(when())
    add(store, 2)
    original = analysis.batch
    monkeypatch.setattr(analysis, "batch", lambda *args: (_ for _ in ()).throw(RuntimeError("private-response")))
    assert pilot(prepared).tick(when("2026-10-28"))["diagnostic"] == "batch_lookup_failed"
    monkeypatch.setattr(analysis, "batch", lambda *args: None)
    assert pilot(prepared).tick(when("2026-10-28"))["diagnostic"] == "observed_batch_missing"
    monkeypatch.setattr(analysis, "batch", original)
    assert len(jobs(analysis)) == 1


def test_missing_intent_for_existing_batch_cannot_create_another(prepared):
    store, analysis, _, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    p.tick(when())
    intent(p)[0].unlink()
    add(store, 2)
    result = pilot(prepared).tick(when("2026-10-28"))
    assert result["diagnostic"] == "batch_without_intent" and len(jobs(analysis)) == 1


def test_failures_are_reported_and_no_automatic_retry(prepared):
    store, analysis, provider, _ = prepared
    add(store, 1)
    p = pilot(prepared)
    p.tick(when())
    provider.fail = True
    drain(analysis)
    today = p.tick(when(hour=17))["report"]
    assert today["failed"] == 1 and today["analysed_images"] == 0 and today["state"] == "finished"
    tomorrow = p.tick(when("2026-10-28"))["report"]
    assert tomorrow["skip_reasons"] == {"retry_required": 1}
    assert provider.calls == 1 and len(jobs(analysis)) == 1


def test_human_edit_or_exclusion_removes_stale_waiting_review_count(prepared):
    store, analysis, _, _ = prepared
    first, second = add(store, 1), add(store, 2)
    p = pilot(prepared)
    p.tick(when())
    drain(analysis)
    assert p.tick(when(hour=17))["report"]["requires_human_review"] == 2
    store.review(first["id"], {"expected_revision": first["revision"], "status": "excluded", "reason": "人工排除"}, "reviewer", "admin")
    store.review(second["id"], {"expected_revision": second["revision"], "description": "人工更新当前版本"}, "reviewer", "admin")
    report = p.tick(when(hour=18))["report"]
    assert report["requires_human_review"] == 0 and report["analysed_images"] == 2
    assert report["reviewed_count"] is None


def test_policy_change_after_intent_does_not_expand_batch(prepared):
    store, analysis, _, path = prepared
    add(store, 1)
    p = pilot(prepared)
    p.tick(when())
    path.write_text(json.dumps({**POLICY, "target": 100}), encoding="utf-8")
    result = pilot(prepared).tick(when(hour=17))
    assert result["diagnostic"] == "policy_changed_after_intent" and len(jobs(analysis)) == 1
