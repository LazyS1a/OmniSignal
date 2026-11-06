"""Prepare a dedicated local profile; optionally open it for USER login.

This is not a collector and does not inspect cookies, page content, account
details, storage_state, or credentials. Run `login` yourself in a local terminal.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omnisignal.knowledge.release import _lock
from omnisignal.workbench.browser_collection import BrowserCollectionError, prepare_profile


def _profile(directory):
    root = prepare_profile(directory)
    chromium = root / "chromium"
    # The marker only establishes ownership of root. A nested link must never
    # redirect the actual browser into an existing personal user-data directory.
    if chromium.is_symlink() or chromium.resolve() != chromium.absolute():
        raise BrowserCollectionError("profile_unmanaged")
    if chromium.exists() and not chromium.is_dir():
        raise BrowserCollectionError("profile_unmanaged")
    return root, chromium


def prepare(directory):
    root, _ = _profile(directory)
    with _lock(root):
        return {"state": "profile_prepared", "playwright_installed": importlib.util.find_spec("playwright") is not None,
                "browser_started": False, "login_verified": False, "collection_verified": False}


def login(directory):
    if not sys.stdin.isatty():
        return {"state": "interactive_terminal_required", "browser_started": False}, 2
    if importlib.util.find_spec("playwright") is None:
        return {"state": "browser_unavailable", "browser_started": False}, 2
    from playwright.sync_api import sync_playwright

    root, chromium = _profile(directory)
    with _lock(root):
        with sync_playwright() as runtime:
            # Normal visible browser. No imported cookies, fingerprint changes,
            # proxy rotation, stealth options, CDP attachment or login automation.
            context = runtime.chromium.launch_persistent_context(str(chromium), headless=False,
                chromium_sandbox=True, accept_downloads=False)
            try:
                page = context.new_page()
                for previous in context.pages:
                    if previous is not page:
                        previous.close()
                page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=30000)
                print("请在新打开的专用浏览器中自行正常登录。不要把密码、验证码输入这个终端。")
                print("本脚本不会读取或判断登录状态，也不会采集、分析、发布或启用计划。")
                input("操作结束后回到此终端，直接按 Enter 关闭浏览器并释放会话锁：")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    return {"state": "manual_session_closed", "browser_started": True,
            "login_verified": False, "collection_verified": False}, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Omni Pinterest 专用会话准备；不采集、不读取账号资料。")
    parser.add_argument("action", choices=("prepare", "login"),
                        help="prepare 只建空专用目录；login 由用户在本机终端打开可见浏览器并自行登录")
    parser.add_argument("--profile-dir", required=True, type=Path,
                        help="全新空目录或 Omni 自己标记的专用目录的绝对路径；不要填现有个人浏览器目录")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            result, code = prepare(args.profile_dir), 0
        else:
            result, code = login(args.profile_dir)
    except BrowserCollectionError as error:
        result, code = {"state": error.code}, 2
    except (EOFError, KeyboardInterrupt):
        result, code = {"state": "operator_closed"}, 130
    except Exception:
        # Chromium errors can contain local paths or account UI text. Do not
        # expose their raw messages, traceback, or browser command-line flags.
        result, code = {"state": "session_unavailable", "hint": "请确认依赖、浏览器组件及专用目录未被其他进程占用。"}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
