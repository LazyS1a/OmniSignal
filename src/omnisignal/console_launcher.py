"""Supervise the local API and Streamlit console behind one Windows entry point."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import BinaryIO
from urllib.request import ProxyHandler, build_opener

from sqlalchemy.engine import URL

from omnisignal.api.auth import ControlPrincipal, hash_control_token, load_control_principals


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "omnisignal.db"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "artifacts" / "private" / "console"


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError("the local console is already running") from exc
        self.handle = handle

    def close(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def atomic_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_http(url: str, process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("a console component exited during startup")
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    response.read(4096)
                    return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("a console component did not become ready in time")


class WindowsKillJob:
    """Kill child services if the hidden supervisor is terminated unexpectedly."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        self.handle = handle

    def add(self, process: subprocess.Popen[bytes]) -> None:
        if self.handle is None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not kernel32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process._handle)):  # type: ignore[attr-defined]
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def close(self) -> None:
        if self.handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self.handle)
            self.handle = None


class ConsoleSupervisor:
    def __init__(self, *, database: Path, runtime_root: Path, api_port: int, ui_port: int) -> None:
        self.database = database.resolve()
        self.runtime_root = runtime_root.resolve()
        self.api_port = api_port
        self.ui_port = ui_port
        self.state_path = self.runtime_root / "runtime.json"
        self.lock = InstanceLock(self.runtime_root / "console.lock")
        self.stop_event = threading.Event()
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[BinaryIO] = []
        self.job: WindowsKillJob | None = None
        self.console_token = secrets.token_urlsafe(48)

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        configured = list(load_control_principals(environment.get("OMNISIGNAL_CONTROL_PRINCIPALS_JSON")))
        if len(configured) >= 50:
            raise RuntimeError("control principal capacity is exhausted")
        configured.append(ControlPrincipal(
            actor=f"local-console-{os.getpid()}", role="operator",
            token_sha256=hash_control_token(self.console_token),
        ))
        environment.update({
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONUTF8": "1",
            "OMNISIGNAL_ENV": "development",
            "OMNISIGNAL_DATABASE_URL": URL.create(
                "sqlite+pysqlite", database=str(self.database)
            ).render_as_string(hide_password=False),
            "OMNISIGNAL_API_BASE_URL": f"http://127.0.0.1:{self.api_port}",
            "OMNISIGNAL_API_PORT": str(self.api_port),
            "OMNISIGNAL_UI_PORT": str(self.ui_port),
            "OMNISIGNAL_LOCAL_CONSOLE_TOKEN": self.console_token,
            "OMNISIGNAL_CONTROL_PRINCIPALS_JSON": json.dumps([
                {"actor": item.actor, "role": item.role, "token_sha256": item.token_sha256}
                for item in configured
            ]),
            "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        })
        return environment

    def state(self, status: str, **extra: object) -> None:
        atomic_json(self.state_path, {
            "schema_version": "1.0",
            "status": status,
            "supervisor_pid": os.getpid(),
            "api_port": self.api_port,
            "ui_port": self.ui_port,
            "ui_url": f"http://127.0.0.1:{self.ui_port}",
            "database_name": self.database.name,
            **extra,
        })

    def migrate(self, environment: dict[str, str], log_root: Path) -> None:
        with (log_root / "migration.stdout.log").open("wb") as output, (
            log_root / "migration.stderr.log"
        ).open("wb") as error:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", str(PROJECT_ROOT / "alembic.ini"), "upgrade", "head"],
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=output,
                stderr=error,
                timeout=120,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError("database migration failed; inspect the console migration log")

    def start_child(self, name: str, arguments: list[str], environment: dict[str, str], log_root: Path) -> subprocess.Popen[bytes]:
        output = (log_root / f"{name}.stdout.log").open("ab")
        error = (log_root / f"{name}.stderr.log").open("ab")
        self.logs.extend((output, error))
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(arguments, cwd=PROJECT_ROOT, env=environment, stdout=output, stderr=error, creationflags=creationflags)
        self.processes.append(process)
        assert self.job is not None
        self.job.add(process)
        return process

    def run(self) -> int:
        self.lock.acquire()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
        log_root = self.runtime_root / "logs" / stamp
        log_root.mkdir(parents=True, exist_ok=False)
        self.state("starting", log_directory=str(log_root), started_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            if self.api_port == self.ui_port or not all(port_available(port) for port in (self.api_port, self.ui_port)):
                raise RuntimeError(f"console port {self.api_port} or {self.ui_port} is already in use")
            self.database.parent.mkdir(parents=True, exist_ok=True)
            environment = self.environment()
            self.migrate(environment, log_root)
            self.job = WindowsKillJob()
            api = self.start_child("api", [sys.executable, "-m", "uvicorn", "omnisignal.api.app:create_app", "--factory",
                "--host", "127.0.0.1", "--port", str(self.api_port)], environment, log_root)
            wait_http(f"http://127.0.0.1:{self.api_port}/health/ready", api, 40)
            ui = self.start_child("ui", [sys.executable, "-m", "streamlit", "run",
                str(PROJECT_ROOT / "src/omnisignal/ops_ui/app.py"), "--server.address", "127.0.0.1",
                "--server.port", str(self.ui_port), "--server.headless", "true",
                "--browser.gatherUsageStats", "false"], environment, log_root)
            wait_http(f"http://127.0.0.1:{self.ui_port}/_stcore/health", ui, 40)
            self.state("ready", api_pid=api.pid, ui_pid=ui.pid, log_directory=str(log_root),
                       started_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            while not self.stop_event.wait(0.5):
                if any(process.poll() is not None for process in self.processes):
                    raise RuntimeError("a console component exited unexpectedly; inspect the D drive logs")
            return 0
        except Exception as exc:
            self.state("failed", error_code=type(exc).__name__, message=str(exc), log_directory=str(log_root))
            return 2
        finally:
            for process in reversed(self.processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            if self.job is not None:
                self.job.close()
            for handle in self.logs:
                handle.close()
            self.lock.close()
            if self.stop_event.is_set():
                self.state("stopped", stopped_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the local OmniSignal control console")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--api-port", type=int, choices=range(1024, 65536), default=8010)
    parser.add_argument("--ui-port", type=int, choices=range(1024, 65536), default=8501)
    args = parser.parse_args()
    supervisor = ConsoleSupervisor(database=args.database, runtime_root=args.runtime_root,
                                   api_port=args.api_port, ui_port=args.ui_port)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: supervisor.stop_event.set())
    try:
        return supervisor.run()
    except AlreadyRunningError:
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
