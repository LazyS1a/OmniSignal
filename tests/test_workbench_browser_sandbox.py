"""Sandbox startup failure must never be retried with isolation disabled."""
from types import ModuleType, SimpleNamespace
import sys

import pytest

from omnisignal.workbench.browser_collection import _PlaywrightBrowser


def test_sandbox_launch_failure_stops_transport_without_insecure_fallback(tmp_path, monkeypatch):
    calls = []
    stopped = []

    def launch(directory, **options):
        calls.append(options)
        raise RuntimeError('sandbox unavailable')

    runtime = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=lambda: stopped.append(True))
    api = ModuleType('playwright.sync_api')
    api.sync_playwright = lambda: SimpleNamespace(start=lambda: runtime)
    monkeypatch.setitem(sys.modules, 'playwright', ModuleType('playwright'))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', api)
    with pytest.raises(RuntimeError, match='sandbox unavailable'):
        _PlaywrightBrowser(tmp_path, lambda *_: True)
    assert len(calls) == 1 and calls[0]['chromium_sandbox'] is True
    assert not any('no-sandbox' in arg for arg in calls[0]['args'])
    assert calls[0]['service_workers'] == 'block'
    assert stopped == [True]
