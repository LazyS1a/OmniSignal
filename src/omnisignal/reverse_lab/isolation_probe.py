"""Runtime assertions for the offline reverse-lab gate container."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket


def main() -> int:
    probe_path = Path("/app/.reverse_lab_write_probe")
    root_read_only = False
    try:
        probe_path.write_text("must not be writable", encoding="utf-8")
    except OSError:
        root_read_only = True
    else:
        probe_path.unlink(missing_ok=True)

    network_blocked = False
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1):
            pass
    except OSError:
        network_blocked = True

    sensitive_environment_absent = not any(
        key in os.environ
        for key in (
            "OMNISIGNAL_DATABASE_URL",
            "OMNISIGNAL_DB_PASSWORD",
            "POSTGRES_PASSWORD",
        )
    )
    non_root = getattr(os, "geteuid", lambda: 0)() != 0
    checks = {
        "network_blocked": network_blocked,
        "non_root": non_root,
        "root_read_only": root_read_only,
        "sensitive_environment_absent": sensitive_environment_absent,
    }
    print(json.dumps(checks, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
