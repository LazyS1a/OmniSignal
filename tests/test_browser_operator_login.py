"""Offline operator-session checks; no real website, credentials or browser."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
import yaml


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pinterest_browser_session as session
import browser_login_desktop as desktop
from omnisignal.knowledge.release import _lock, KnowledgeError


class LandingTimeout(Exception):
    pass


@pytest.fixture
def browser(monkeypatch):
    seen = {"closed": False, "goto": [], "input": 0, "launch": []}
    page = SimpleNamespace(goto=lambda url, **kw: seen["goto"].append((url, kw)))
    context = SimpleNamespace(pages=[], new_page=lambda: page,
                              close=lambda: seen.update(closed=True))
    def launch(path, **options):
        seen["launch"].append((path, options))
        return context
    class Controller:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        def __exit__(self, *_):
            return False
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(
        sync_playwright=Controller, TimeoutError=LandingTimeout))
    original_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "playwright" else original_spec(name))
    monkeypatch.setattr(session.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def finish(_):
        assert seen["closed"] is False
        seen["input"] += 1
    monkeypatch.setattr("builtins.input", finish)
    return seen, page


@pytest.mark.parametrize("source", tuple(session.SOURCE_URLS))
def test_each_source_uses_normal_landing_page_and_sandbox(tmp_path, browser, source):
    seen, _ = browser
    result, code = session.login(tmp_path / source, source)
    assert code == 0 and result["login_verified"] is False
    assert result["collection_verified"] is False
    assert seen["goto"] == [(session.SOURCE_URLS[source], {"wait_until": "domcontentloaded", "timeout": 30000})]
    assert seen["launch"][0][0] == str(tmp_path / source / "chromium")
    assert seen["launch"][0][1] == {"headless": False, "chromium_sandbox": True, "accept_downloads": False}
    assert seen["input"] == 1 and seen["closed"] is True


def test_landing_timeout_keeps_browser_open_for_user_without_retry(tmp_path, browser, monkeypatch, capsys):
    seen, page = browser
    def timeout(url, **options):
        seen["goto"].append((url, options))
        raise LandingTimeout("synthetic private navigation detail")
    monkeypatch.setattr(page, "goto", timeout)
    result, code = session.login(tmp_path / "pinterest")
    assert code == 0 and result["state"] == "manual_session_closed"
    assert len(seen["goto"]) == 1 and seen["input"] == 1 and seen["closed"]
    output = capsys.readouterr().out
    assert "浏览器保持打开" in output and "private navigation" not in output


def test_user_session_keeps_exclusive_profile_lock_until_close(tmp_path, browser, monkeypatch):
    directory = tmp_path / "pinterest"
    def finish(_):
        with pytest.raises(KnowledgeError, match="locked"):
            with _lock(directory):
                pytest.fail("profile lock was not exclusive")
    monkeypatch.setattr("builtins.input", finish)
    assert session.login(directory)[1] == 0
    with _lock(directory):
        pass


def test_sources_keep_separate_profiles_and_reject_unknown_source(tmp_path, browser):
    seen, _ = browser
    assert session.login(tmp_path / "pinterest", "pinterest")[1] == 0
    seen["closed"] = False
    assert session.login(tmp_path / "huaban", "huaban")[1] == 0
    assert seen["launch"][0][0] != seen["launch"][1][0]
    assert session.login(tmp_path / "unknown", "../unknown")[0]["state"] == "unsupported_source"
    assert len(seen["launch"]) == 2 and not (tmp_path / "unknown").exists()


@pytest.mark.parametrize("source", ("huaban", "zcool", "behance"))
def test_prepared_and_logged_profile_is_accepted_by_same_source_collector(tmp_path, browser, source):
    from omnisignal.workbench.project_browser import prepare_project_profile
    directory = tmp_path / source
    assert session.prepare(directory, source)["login_verified"] is False
    assert session.login(directory, source)[1] == 0
    assert prepare_project_profile(directory, source) == directory
    marker = directory / ".omnisignal-project-profile.json"
    before = marker.read_bytes()
    other = "zcool" if source != "zcool" else "huaban"
    with pytest.raises(session.BrowserCollectionError, match="profile_unmanaged"):
        session._profile(directory, other)
    assert marker.read_bytes() == before


def test_legacy_profile_is_preserved_instead_of_implicitly_reassigned(tmp_path):
    directory = tmp_path / "legacy-zcool"
    session.prepare(directory)
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(session.BrowserCollectionError, match="profile_unmanaged"):
        session.prepare(directory, "zcool")
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == before


def test_login_requires_interactive_terminal_before_launch(tmp_path, browser, monkeypatch):
    seen, _ = browser
    monkeypatch.setattr(session.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert session.login(tmp_path / "pinterest")[0]["state"] == "interactive_terminal_required"
    assert not seen["launch"]


@pytest.mark.parametrize("source", tuple(session.SOURCE_URLS))
def test_desktop_passes_source_and_preserves_bounded_non_root_vnc(tmp_path, monkeypatch, source):
    commands = []
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="posix", getuid=lambda: 1000,
        environ={"HOME": "/tmp/omnisignal-test"}, killpg=lambda *_: None))
    monkeypatch.setattr(desktop.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    # Simulate only the desktop dependencies; do not create /tmp or launch tools.
    original_exists = Path.exists
    monkeypatch.setattr(Path, "mkdir", lambda *_, **__: None)
    monkeypatch.setattr(Path, "exists", lambda p: True if str(p).replace("\\", "/") == "/tmp/.X11-unix/X99" else original_exists(p))
    def popen(command, **options):
        commands.append((command, options))
        return SimpleNamespace(pid=len(commands), poll=lambda: None, wait=lambda **_: 0)
    monkeypatch.setattr(desktop.subprocess, "Popen", popen)
    assert desktop.main(["--source", source, "--profile-dir", str(tmp_path / source)]) == 0
    login = commands[-1][0]
    assert login[-2:] == ["--source", source]
    assert commands[-1][1]["start_new_session"] is True
    vnc = next(command for command, _ in commands if command[0] == "x11vnc")
    assert "-localhost" in vnc and "-nopw" in vnc
    assert commands[-1][1]["env"]["DISPLAY"] == ":99"


def test_compose_exposes_only_loopback_and_only_selected_source_profile():
    path = SCRIPTS.parent / "deploy/compose.browser-login.yaml"
    service = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["browser-login"]
    assert service["ports"] == ["127.0.0.1:6080:6080"]
    assert service["read_only"] and service["stdin_open"] and service["tty"]
    assert len(service["volumes"]) == 1
    volume = service["volumes"][0]
    assert volume["source"].endswith("/${OMNISIGNAL_BROWSER_SOURCE:-pinterest}")
    assert volume["target"] == "/app/browser-sessions/${OMNISIGNAL_BROWSER_SOURCE:-pinterest}"
    assert volume["bind"]["create_host_path"] is False
    assert service["command"][-2:] == ["--max-seconds", "1200"]
