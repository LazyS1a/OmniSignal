"""Offline regression for the Chromium sandbox verdict; no browser/network."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

spec = importlib.util.spec_from_file_location(
    "browser_runtime_smoke", Path(__file__).resolve().parents[1] / "scripts/browser_runtime_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize("text", [
    "You are adequately sandboxed.",
    "Namespace sandbox Yes\nSeccomp-BPF sandbox Yes\n  You   are adequately sandboxed.  ",
])
def test_exact_positive_sandbox_verdict(text):
    assert smoke.sandbox_status_confirmed(text)


@pytest.mark.parametrize("text", [
    "You are not adequately sandboxed.",
    "You are not sandboxed.",
    "You are adequately sandboxed.\nYou are not adequately sandboxed.",
    "You are NOT adequately sandboxed.\nYou are adequately sandboxed.",
    "You are adequately sandboxed.\nSandbox is disabled",
    "Example text says You are adequately sandboxed.",
    "Namespace sandbox Yes\nSeccomp-BPF sandbox Yes",
    "", None,
])
def test_negative_missing_or_conflicting_verdict_is_rejected(text):
    assert not smoke.sandbox_status_confirmed(text)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    state = {"launches": [], "closed": [], "visits": [], "verdict": "You are adequately sandboxed.",
             "render": "ready", "fail_full_launch": False, "live_config": None}
    def launch(profile, **options):
        index = len(state["launches"])
        state["launches"].append((profile, options))
        if index == 1:
            assert state["closed"] == [0], "Release the shell before launching full Chromium"
            if state["fail_full_launch"]:
                raise RuntimeError("synthetic full Chromium launch failure")
        def goto(url):
            assert index == 1, "Default headless shell must never visit chrome://sandbox"
            state["visits"].append((index, url))
        page = SimpleNamespace(set_content=lambda _: None, goto=goto,
            locator=lambda selector: SimpleNamespace(inner_text=lambda: state["render"] if selector == "#result" else state["verdict"]),
            evaluate=lambda _: "synthetic HeadlessChrome/130.0.0.0")
        return SimpleNamespace(new_page=lambda: page, close=lambda: state["closed"].append(index))
    class Controller:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        def __exit__(self, *_):
            return False
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Controller))
    monkeypatch.setitem(sys.modules, "omnisignal.workbench.browser_jobs", SimpleNamespace(browser_config=lambda _: state["live_config"]))
    monkeypatch.setattr(smoke.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(smoke.importlib.metadata, "version", lambda _: "1.62.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return state


def test_shell_render_and_full_chromium_sandbox_are_distinct_required_checks(runtime):
    result = smoke.main()
    assert result["passed"] and result["headless_shell_rendered"]
    assert result["sandbox_verification"] == "full_chromium_chrome_sandbox_positive_verdict"
    assert result["collection_calls"] == result["model_calls"] == 0 and not result["live_profile_used"]
    assert len(runtime["launches"]) == 2 and runtime["closed"] == [0, 1]
    assert runtime["visits"] == [(1, "chrome://sandbox")]
    shell, full = runtime["launches"]
    assert shell[0] != full[0] and "channel" not in shell[1] and full[1]["channel"] == "chromium"
    for _, options in runtime["launches"]:
        assert options["headless"] and options["chromium_sandbox"] and not options["accept_downloads"]
        assert options["service_workers"] == "block"


@pytest.mark.parametrize("verdict", ["You are not adequately sandboxed.", "", "You are adequately sandboxed.\nSandbox is disabled"])
def test_full_chromium_negative_or_missing_verdict_does_not_pass(runtime, verdict):
    runtime["verdict"] = verdict
    with pytest.raises(AssertionError, match="chromium_sandbox_not_confirmed"):
        smoke.main()
    assert runtime["closed"] == [0, 1]


def test_shell_render_failure_does_not_skip_to_full_browser(runtime):
    runtime["render"] = "failed"
    with pytest.raises(AssertionError, match="browser_render_failed"):
        smoke.main()
    assert len(runtime["launches"]) == 1 and runtime["closed"] == [0]


def test_full_browser_launch_failure_is_not_treated_as_sandbox_success(runtime):
    runtime["fail_full_launch"] = True
    with pytest.raises(RuntimeError, match="full Chromium launch failure"):
        smoke.main()
    assert runtime["closed"] == [0]


def test_live_configuration_is_still_rejected(runtime):
    runtime["live_config"] = {"synthetic": True}
    with pytest.raises(AssertionError, match="live_browser_configuration_must_not_be_mounted"):
        smoke.main()
    assert runtime["closed"] == [0, 1]


def test_root_user_is_still_rejected_before_starting_a_browser(runtime, monkeypatch):
    monkeypatch.setattr(smoke.os, "getuid", lambda: 0)
    with pytest.raises(AssertionError, match="non_root_user_required"):
        smoke.main()
    assert runtime["launches"] == []
