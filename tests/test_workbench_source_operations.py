import io
import json
from datetime import datetime, timezone

from PIL import Image

from omnisignal.workbench.source_operations import operational_sources, SESSION_PREFIX
from omnisignal.workbench.store import Store

NOW = datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc).timestamp()
CATALOG = [{"id": "pinterest", "label": "Pinterest", "category": "supplemental", "verified": True}]


def setup_store(tmp_path):
    return Store(tmp_path, clock=lambda: NOW, recover_publications=False)


def state(store):
    return operational_sources(store, catalog=CATALOG)[0]["operations"]


def plan(store, ident="p1", enabled=True):
    with store.db() as db:
        db.execute("INSERT INTO plans VALUES(?,?,?,1,0,NULL)", (ident, json.dumps({"source_id": "pinterest"}), int(enabled)))


def session(store, value):
    with store.db() as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (SESSION_PREFIX + "pinterest", json.dumps(value)))


def picture(n=1):
    out = io.BytesIO()
    Image.new("RGB", (100, 100), (n, 20, 30)).save(out, "PNG")
    return out.getvalue()


def asset(store, number, actor, content=None):
    row = store.import_asset({"title": "isolated source status fixture", "source_id": "pinterest", "source_url": f"https://example.invalid/{number}"}, content or picture(number), actor)
    with store.db() as db:
        db.execute("UPDATE assets SET created_at=? WHERE id=?", ("2026-11-05T17:00:00+00:00", row["id"]))
    return row


def test_static_verified_and_manual_sample_never_imply_running_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "500")
    store = setup_store(tmp_path)
    asset(store, 1, "operator")
    before = store.counts()
    result = state(store)
    assert result["state"] == "no_plans"
    assert result["plan_count"] == result["enabled_plan_count"] == 0
    assert result["session"]["state"] == "not_configured"
    assert result["today"] == {"day": "2026-11-06", "timezone": "Asia/Shanghai", "imported": 0, "scope": "collector_distinct_image_content"}
    assert result["continuous_collection_verified"] is None
    assert result["collection_evidence"]["level"] == "none"
    assert store.counts() == before


def test_daily_count_is_collector_distinct_hash_and_plan_records_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "500")
    store = setup_store(tmp_path)
    plan(store);plan(store, "p2", enabled=False)
    asset(store, 1, "collector", picture(1));asset(store, 2, "collector", picture(1));asset(store, 3, "operator")
    with store.db() as db:
        db.execute("INSERT INTO runs(id,plan_id,status,started_at,finished_at,imported) VALUES('r1','p1','ok',?,?,2)", ("2026-11-05T17:00:00Z", "2026-11-05T17:01:00Z"))
    result = state(store)
    assert result["today"]["imported"] == 1
    assert result["plan_count"] == 2 and result["enabled_plan_count"] == 1
    assert result["last_success_at"] == "2026-11-05T17:01:00Z"
    assert result["collection_evidence"] == {"successful_runs": 1, "successful_days": 1, "level": "limited_runs"}
    assert result["continuous_collection_verified"] is None


def test_browser_status_requires_fresh_check_and_never_leaks_metadata(tmp_path):
    store = setup_store(tmp_path);plan(store)
    session(store, {"state": "ready", "checked_at": "2026-11-05T17:00:00Z", "expires_at": "2026-11-05T18:00:00Z", "token": "private-session-fixture", "reason_code": "private-session-fixture"})
    result = state(store)
    assert result["session"]["state"] == "ready"
    assert "private-session-fixture" not in json.dumps(result)
    session(store, {"state": "ready", "checked_at": "2026-11-05T16:00:00Z"})
    assert state(store)["session"]["state"] == "unknown"
    session(store, {"state": "ready", "checked_at": "2026-11-05T17:00:00Z", "expires_at": "2026-11-05T16:59:59Z"})
    assert state(store)["session"]["state"] == "expired"
    session(store, {"state": "ready", "checked_at": "2026-11-05T18:00:00Z"})
    assert state(store)["session"]["state"] == "unknown"
    session(store, {"state": "ready"})
    assert state(store)["session"]["state"] == "unknown"


def test_failure_allowlist_and_invalid_quota_do_not_leak_raw_diagnostics(tmp_path, monkeypatch):
    store = setup_store(tmp_path);plan(store)
    with store.db() as db:
        db.execute("INSERT INTO runs(id,plan_id,status,started_at,reason) VALUES('r1','p1','failed',?,?)", ("2026-11-05T17:00:00Z", "private-token-upstream"))
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "private-bad-config")
    result = state(store)
    assert result["quota"] is None and result["quota_error"]
    assert result["today"]["imported"] is None
    assert result["last_failure"]["code"] == "collection_not_completed"
    assert "private-" not in json.dumps(result)
    with store.db() as db:
        db.execute("UPDATE runs SET reason='access_barrier' WHERE id='r1'")
    assert state(store)["last_failure"]["code"] == "access_barrier"


def test_configured_api_presence_is_unknown_without_connection_proof(tmp_path, monkeypatch):
    store = setup_store(tmp_path)
    monkeypatch.setenv("OMNISIGNAL_PEXELS_API_KEY_FILE", "not-opened-private-path")
    result = operational_sources(store, catalog=[{"id": "pexels", "verified": True}])[0]["operations"]
    assert result["session"]["state"] == "unknown"
    assert "not-opened" not in json.dumps(result)


def test_browser_transport_without_observation_is_unknown_not_missing(tmp_path):
    store = setup_store(tmp_path)
    result = operational_sources(store, catalog=[{**CATALOG[0], "readiness": {"transport": "browser"}}])[0]["operations"]
    assert result["session"]["state"] == "unknown"
    assert result["session"]["reason_code"] == "not_checked"
