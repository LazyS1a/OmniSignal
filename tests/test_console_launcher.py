from __future__ import annotations

import json
import os
from pathlib import Path
import socket

import pytest

from omnisignal.console_launcher import (
    AlreadyRunningError,
    ConsoleSupervisor,
    InstanceLock,
    atomic_json,
    port_available,
)
from omnisignal.api.auth import load_control_principals


def test_atomic_state_replaces_complete_document(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    atomic_json(path, {"status": "starting", "supervisor_pid": 1})
    atomic_json(path, {"status": "ready", "supervisor_pid": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "ready", "supervisor_pid": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_instance_lock_rejects_second_supervisor_and_recovers(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "console.lock")
    second = InstanceLock(tmp_path / "console.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


def test_port_probe_detects_owned_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert port_available(port) is False
    assert port_available(port) is True


def test_supervisor_environment_is_local_and_keeps_paths_on_project_drive(tmp_path: Path) -> None:
    supervisor = ConsoleSupervisor(database=tmp_path / "console.db", runtime_root=tmp_path / "runtime",
                                   api_port=18010, ui_port=18501)
    environment = supervisor.environment()
    assert environment["OMNISIGNAL_ENV"] == "development"
    assert "console.db" in environment["OMNISIGNAL_DATABASE_URL"]
    assert environment["OMNISIGNAL_API_BASE_URL"] == "http://127.0.0.1:18010"
    assert environment["PYTHONPATH"].endswith(os.path.join("OmniSignal", "src"))
    principals = load_control_principals(environment["OMNISIGNAL_CONTROL_PRINCIPALS_JSON"])
    assert len(principals) == 1 and principals[0].role == "operator"
    assert environment["OMNISIGNAL_LOCAL_CONSOLE_TOKEN"] == supervisor.console_token
    supervisor.state("testing")
    assert supervisor.console_token not in supervisor.state_path.read_text(encoding="utf-8")


def test_same_ports_fail_before_migration_and_leave_safe_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = ConsoleSupervisor(database=tmp_path / "console.db", runtime_root=tmp_path / "runtime",
                                   api_port=18010, ui_port=18010)
    monkeypatch.setattr(supervisor, "migrate", lambda *args: pytest.fail("migration should not run"))
    assert supervisor.run() == 2
    state = json.loads(supervisor.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["error_code"] == "RuntimeError"
    assert "18010" in state["message"]
