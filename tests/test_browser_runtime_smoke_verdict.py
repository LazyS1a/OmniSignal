"""Offline regression for the Chromium sandbox verdict; no browser/network."""
import importlib.util
from pathlib import Path

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
