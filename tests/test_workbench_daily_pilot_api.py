"""Daily status is authenticated and read-only; scheduling belongs to the worker."""
import json

from fastapi.testclient import TestClient

from omnisignal.workbench.app import create_app
from omnisignal.workbench.auth import password_hash


def test_daily_status_requires_login_and_does_not_tick_or_create_work(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_DAILY_PILOT_CONFIG", raising=False)
    app = create_app(tmp_path / "workbench", start_worker=False)
    (app.state.store.root / "users.json").write_text(json.dumps([
        {"username": "viewer", "role": "viewer", "password_hash": password_hash("synthetic-daily-status-password")}
    ]), encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must never plan or enqueue")
    monkeypatch.setattr(app.state.daily_pilot, "tick", forbidden)
    with TestClient(app) as client:
        assert client.get("/workbench/api/analysis/daily-pilot").status_code == 401
        assert client.post("/workbench/api/session", json={
            "username": "viewer", "password": "synthetic-daily-status-password"
        }).status_code == 200
        for _ in range(3):
            response = client.get("/workbench/api/analysis/daily-pilot")
            assert response.status_code == 200
            assert response.json()["state"] == "disabled"
            assert response.json()["worker_enabled"] is False
        with app.state.store.db() as db:
            assert db.execute("SELECT count(*) FROM analysis_jobs").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM analysis_batches").fetchone()[0] == 0
        assert not app.state.daily_pilot.root.exists()


def test_worker_lifecycle_stops_scheduler_before_analysis(tmp_path, monkeypatch):
    from omnisignal.workbench.analysis import AnalysisService
    from omnisignal.workbench.collection import WorkbenchCollector
    from omnisignal.workbench.daily_pilot import DailyPilot
    events = []
    for cls, name in ((WorkbenchCollector, "collector"), (AnalysisService, "analysis"), (DailyPilot, "pilot")):
        monkeypatch.setattr(cls, "start", lambda self, name=name: events.append(name + ":start"))
        monkeypatch.setattr(cls, "stop", lambda self, name=name: events.append(name + ":stop"))
    app = create_app(tmp_path / "workbench", start_worker=True)
    with TestClient(app):
        assert events == ["collector:start", "analysis:start", "pilot:start"]
    assert events == ["collector:start", "analysis:start", "pilot:start", "pilot:stop", "analysis:stop", "collector:stop"]
