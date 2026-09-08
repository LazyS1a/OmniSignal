import json
from pathlib import Path
import socket
import threading
import time

import httpx
import pytest
import respx
import uvicorn

from omnisignal.api import create_app
from omnisignal.config import Environment, Settings
from omnisignal.operations.health import check_once, main
from omnisignal.ops_ui.client import OpsApiClient
from omnisignal.storage import upgrade_database


BASE = "http://127.0.0.1:8010"


def _healthy(router, *, stopped=0):
    router.get(BASE + "/health/live").respond(200, json={"status": "alive"})
    router.get(BASE + "/health/ready").respond(200, json={"status": "ready"})
    return router.get(BASE + "/ops/summary", params={"window_hours": 24}).respond(200, json={
        "ingestion_runs": {"total": 3, "stopped": stopped},
        "records": {"active_ingested": 7, "normalized_versions": 8},
        "unfinished_ingestion": {"total": 0, "needs_review": 0, "review_after_hours": 24, "scope": "all_history"},
        "unexpected_private_field": "do-not-copy-fixture-content",
    })


def test_healthy_check_only_returns_selected_fields(respx_mock):
    _healthy(respx_mock)
    result = check_once()
    assert result["status"] == "checks_passed" and result["exit_code"] == 0
    assert len(respx_mock.calls) == 3
    assert "do-not-copy" not in json.dumps(result)
    assert all(call.request.method == "GET" and "authorization" not in call.request.headers for call in respx_mock.calls)


def test_recent_stopped_runs_warn_without_claiming_current_outage(respx_mock):
    _healthy(respx_mock, stopped=1)
    result = check_once()
    assert result["exit_code"] == 1
    assert result["warnings"] == ["stopped_runs_in_24h"]
    assert all(check["status"] == "passed" for check in result["checks"])


@pytest.mark.parametrize("aged, expected", [(0, 0), (1, 1)])
def test_unfinished_runs_only_warn_when_aged(respx_mock, aged, expected):
    summary = _healthy(respx_mock)
    summary.respond(200, json={
        "ingestion_runs": {"total": 0, "stopped": 0},
        "records": {"active_ingested": 0, "normalized_versions": 0},
        "unfinished_ingestion": {"total": 1, "needs_review": aged, "review_after_hours": 24, "scope": "all_history"},
    })
    report = check_once()
    assert report["exit_code"] == expected
    assert ("unfinished_runs_need_review" in report["warnings"]) == bool(aged)


def test_old_api_without_unfinished_observations_is_not_reported_fully_checked(respx_mock):
    summary = _healthy(respx_mock)
    summary.respond(200, json={"ingestion_runs": {"total": 0, "stopped": 0}, "records": {"active_ingested": 0, "normalized_versions": 0}})
    report = check_once()
    assert report["exit_code"] == 2
    assert report["checks"][2]["code"] == "contract_mismatch"


def test_unreachable_service_fails_all_checks_without_retries(respx_mock):
    respx_mock.route().mock(side_effect=httpx.ConnectError("private-host-fixture"))
    result = check_once()
    assert result["exit_code"] == 2 and len(respx_mock.calls) == 3
    assert {check["code"] for check in result["checks"]} == {"transport_error"}
    assert "private-host" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["readiness_503", "redirect", "oversized", "malformed", "bad_contract", "deep_json"])
def test_bad_response_does_not_turn_green(respx_mock, kind):
    summary = _healthy(respx_mock)
    if kind == "readiness_503":
        respx_mock.get(BASE + "/health/ready").respond(503, text="sensitive-error-body")
    elif kind == "redirect":
        respx_mock.get(BASE + "/health/ready").respond(302, headers={"Location": "https://example.invalid"}, json={"status": "ready"})
    elif kind == "oversized":
        summary.respond(200, text="x" * 65537)
    elif kind == "malformed":
        summary.respond(200, text="not-json-sensitive-body")
    elif kind == "deep_json":
        summary.respond(200, text='{"value":' + "[" * 2000 + "0" + "]" * 2000 + "}")
    else:
        summary.respond(200, json={"ingestion_runs": {"total": True, "stopped": 0}, "records": {"active_ingested": 1, "normalized_versions": 1}})
    result = check_once()
    assert result["exit_code"] == 2
    assert "sensitive" not in json.dumps(result)
    assert all(str(call.request.url).startswith(BASE) for call in respx_mock.calls)


def test_health_client_does_not_allow_arbitrary_health_paths():
    with pytest.raises(ValueError):
        OpsApiClient().get_health_json("/health/ready?token=fixture")


def test_cli_report_is_exclusive_and_exit_codes_are_machine_readable(tmp_path: Path, respx_mock, capsys):
    _healthy(respx_mock)
    report = tmp_path / "health.json"
    assert main(["--report", str(report)]) == 0
    original = report.read_bytes()
    assert json.loads(original)["scope"] == "local_api_one_shot"
    assert main(["--report", str(report)]) == 3
    assert report.read_bytes() == original
    assert str(tmp_path) not in capsys.readouterr().out


@pytest.mark.parametrize("args", [{"port": 0}, {"port": 65536}, {"timeout_seconds": 0.1}, {"timeout_seconds": 11}])
def test_invalid_config_fails_before_network(args):
    with pytest.raises(ValueError):
        check_once(**args)


def test_real_local_api_not_ready_then_migration_then_recovery(tmp_path: Path):
    # Actual TCP and FastAPI, with an isolated file-backed test database.
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'health.db').as_posix()}"
    app = create_app(settings=Settings(environment=Environment.TEST, database_url=database_url), control_principals=())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        before = check_once(port=port)
        assert before["checks"][0]["status"] == "passed"
        assert before["checks"][1]["http_status"] == 503
        assert before["exit_code"] == 2
        upgrade_database(database_url)
        after = check_once(port=port)
        assert after["status"] == "checks_passed"
        assert after["observations"]["records"]["active_ingested"] == 0
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
    assert not worker.is_alive()
    assert check_once(port=port, timeout_seconds=0.5)["exit_code"] == 2
