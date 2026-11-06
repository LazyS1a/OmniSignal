"""Human-operated temporary desktop. Expose port 6080 on HOST LOOPBACK only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from pinterest_browser_session import SOURCE_URLS


def stop(process):
    """Terminate only the process group created by this helper, including browsers."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--source", choices=tuple(SOURCE_URLS), default="pinterest")
    args = parser.parse_args(argv)
    if os.name != "posix" or not sys.stdin.isatty():
        print(json.dumps({"state": "linux_interactive_terminal_required"}))
        return 2
    if os.getuid() == 0 or not 60 <= args.max_seconds <= 3600:
        print(json.dumps({"state": "non_root_and_bounded_session_required"}))
        return 2
    if not args.profile_dir.is_absolute() or args.profile_dir.resolve() != args.profile_dir:
        print(json.dumps({"state": "absolute_non_symlink_profile_required"}))
        return 2
    home = Path(os.environ.get("HOME", "/tmp/omnisignal-home"))
    if not home.is_relative_to(Path("/tmp")):
        print(json.dumps({"state": "temporary_home_required"}))
        return 2
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {**os.environ, "DISPLAY": ":99"}
    children = []
    try:
        def launch(command, *, interactive=False):
            child = subprocess.Popen(command, env=env, start_new_session=True,
                stdin=sys.stdin if interactive else subprocess.DEVNULL,
                stdout=None if interactive else subprocess.DEVNULL,
                stderr=None if interactive else subprocess.DEVNULL)
            children.append(child)
            return child
        xvfb = launch(["Xvfb", ":99", "-screen", "0", "1440x900x24", "-nolisten", "tcp", "-ac"])
        for _ in range(50):
            if xvfb.poll() is not None:
                raise RuntimeError("desktop_unavailable")
            if Path("/tmp/.X11-unix/X99").exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("desktop_unavailable")
        launch(["openbox"])
        # VNC itself is container-loopback only; no public RFB/CDP/SSH service.
        launch(["x11vnc", "-display", ":99", "-localhost", "-rfbport", "5901", "-forever", "-shared", "-nopw", "-quiet"])
        launch(["websockify", "--web=/usr/share/novnc", "0.0.0.0:6080", "127.0.0.1:5901"])
        print("仅通过 SSH 转发打开本机 http://127.0.0.1:16080/vnc.html；请自行正常登录网站。", flush=True)
        print(f"本入口最多运行 {args.max_seconds} 秒；结束后在此终端按 Enter。不会读取账号、验证码或 Cookie。", flush=True)
        login = launch([sys.executable, str(Path(__file__).with_name("pinterest_browser_session.py")),
                        "login", "--profile-dir", str(args.profile_dir), "--source", args.source], interactive=True)
        try:
            return login.wait(timeout=args.max_seconds)
        except subprocess.TimeoutExpired:
            print(json.dumps({"state": "operator_session_expired"}))
            return 124
    except (KeyboardInterrupt, EOFError):
        return 130
    except Exception:
        print(json.dumps({"state": "desktop_unavailable"}))
        return 2
    finally:
        for child in reversed(children):
            stop(child)


if __name__ == "__main__":
    raise SystemExit(main())
