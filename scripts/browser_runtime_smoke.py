"""Isolated image smoke: run with --network none and no live/profile mounts."""
import importlib.metadata
import json
import os
from pathlib import Path
import re
import tempfile


def sandbox_status_confirmed(text):
    """Accept the browser's exact positive verdict, never a negated substring."""
    if not isinstance(text, str):
        return False
    lines = [" ".join(line.split()).casefold() for line in text.splitlines()]
    negative = re.compile(r"\bnot\s+(?:adequately\s+)?sandboxed\b|\bsandbox(?:ing)?\s+(?:is\s+)?disabled\b")
    return (lines.count("you are adequately sandboxed.") == 1
            and not any(negative.search(line) for line in lines))


def main():
    assert os.getuid() != 0, "non_root_user_required"
    from playwright.sync_api import sync_playwright
    assert importlib.metadata.version("playwright") == "1.62.0", "locked_browser_version_required"
    Path(os.environ.get("HOME", "/tmp/omnisignal-home")).mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omni-browser-smoke-") as directory:
        with sync_playwright() as runtime:
            # The default headless shell is the collector runtime. It does not
            # expose chrome://sandbox, so verify rendering without that URL.
            browser = runtime.chromium.launch_persistent_context(str(Path(directory) / "render"), headless=True, chromium_sandbox=True,
                accept_downloads=False, service_workers="block")
            try:
                page = browser.new_page()
                page.set_content("<title>Omni isolated browser</title><main id='result'>ready</main>")
                assert page.locator("#result").inner_text() == "ready", "browser_render_failed"
                shell_version = re.search(r"(?:HeadlessChrome|Chrome)/([0-9.]+)", page.evaluate("navigator.userAgent"))
            finally:
                browser.close()
            # Full Chromium's new headless mode supports the internal diagnostic
            # page. This is a separate positive check, never a fallback verdict.
            browser = runtime.chromium.launch_persistent_context(str(Path(directory) / "sandbox"), channel="chromium",
                headless=True, chromium_sandbox=True, accept_downloads=False, service_workers="block")
            try:
                page = browser.new_page()
                page.goto("chrome://sandbox")
                assert sandbox_status_confirmed(page.locator("body").inner_text()), "chromium_sandbox_not_confirmed"
                version = re.search(r"(?:HeadlessChrome|Chrome)/([0-9.]+)", page.evaluate("navigator.userAgent"))
                from omnisignal.workbench.browser_jobs import browser_config
                assert browser_config("pinterest") is None, "live_browser_configuration_must_not_be_mounted"
                return {"passed": True, "playwright": importlib.metadata.version("playwright"),
                        "chromium": version.group(1) if version else "unknown", "non_root": True, "sandbox": True,
                        "headless_shell_rendered": True, "headless_shell_sandbox_requested": True,
                        "headless_shell_chromium": shell_version.group(1) if shell_version else "unknown",
                        "sandbox_verification": "full_chromium_chrome_sandbox_positive_verdict",
                        "collection_calls": 0, "model_calls": 0, "live_profile_used": False}
            finally:
                browser.close()

if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception as error:
        # Raw launch errors can contain machine paths and command lines.
        print(json.dumps({"passed": False, "error_type": type(error).__name__, "reason": "isolated_browser_smoke_failed"}))
        raise SystemExit(1)
