"""Project-owned worker fixture for the authorized Hook protocol."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from omnisignal.connectors.authorized_hook_protocol import OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION


def _write_result(path: Path, document: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    options = job.get("runner_options", {})
    mode = options.get("mode", "ok") if isinstance(options, dict) else "ok"
    if mode == "crash":
        return 17
    if mode == "hang":
        time.sleep(30)
        return 18

    target = str(job.get("target", ""))
    base: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "status": "ok",
        "target": target,
        "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
        "records": [],
        "next_cursor": None,
        "has_more": False,
    }
    if mode in {"unauthorized", "forbidden"}:
        base["status"] = "error"
        base["error"] = {
            "kind": "authentication" if mode == "unauthorized" else "permission",
            "status": 401 if mode == "unauthorized" else 403,
        }
    elif mode == "schema_drift":
        base["schema_fingerprint"] = "0" * 64
    elif mode == "wrong_target":
        base["target"] = "mock-process://outside-scope"
    else:
        cursor = job.get("cursor")
        if cursor is None:
            record = {
                "source_record_id": "fixture-hook-1",
                "published_at": "2026-09-03T00:00:00Z",
                "title": "Synthetic authorized Hook record one",
                "text": "Project-owned fixture output used to validate an isolated worker boundary.",
            }
            base["next_cursor"] = "fixture-page-2"
            base["has_more"] = True
        else:
            record = {
                "source_record_id": "fixture-hook-2",
                "published_at": "2026-09-03T00:01:00Z",
                "title": "Synthetic authorized Hook record two",
                "text": "Second deterministic fixture record for checkpoint and replay verification.",
            }
        if mode == "sensitive_field":
            record["access_token"] = "must-never-cross-boundary"
        if mode == "environment_probe":
            record["text"] = (
                "unexpected environment leak"
                if os.environ.get("OMNISIGNAL_UNRELATED_SECRET")
                else "minimal environment confirmed"
            )
        base["records"] = [record]
    _write_result(args.result, base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
