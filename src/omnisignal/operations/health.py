"""One-shot loopback health check with bounded requests and safe JSON evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from omnisignal.ops_ui.client import OpsApiClient, OpsApiError


def _counts(document: dict, group: str, fields: tuple[str, ...]) -> dict[str, int]:
    values = document.get(group)
    if not isinstance(values, dict):
        raise ValueError("invalid summary")
    result = {}
    for field in fields:
        value = values.get(field)
        if type(value) is not int or value < 0:
            raise ValueError("invalid summary counts")
        result[field] = value
    return result


def check_once(*, port: int = 8010, timeout_seconds: float = 3) -> dict:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not 0.5 <= timeout_seconds <= 10:
        raise ValueError("timeout must be between 0.5 and 10 seconds")
    client = OpsApiClient(base_url=f"http://127.0.0.1:{port}", timeout_seconds=timeout_seconds, max_response_bytes=65536, retries=0)
    checks = []
    counts = {}
    for name, path, expected in (
        ("liveness", "/health/live", "alive"),
        ("readiness", "/health/ready", "ready"),
        ("operations", "/ops/summary", None),
    ):
        started = time.monotonic()
        result = {"name": name, "status": "passed", "code": "ok"}
        try:
            if expected:
                document = client.get_health_json(path)
                if document.get("status") != expected:
                    raise ValueError("unexpected health status")
            else:
                document = client.get_json(path, params={"window_hours": 24})
                runs = _counts(document, "ingestion_runs", ("total", "stopped"))
                records = _counts(document, "records", ("active_ingested", "normalized_versions"))
                unfinished = _counts(document, "unfinished_ingestion", ("total", "needs_review", "review_after_hours"))
                if runs["stopped"] > runs["total"]:
                    raise ValueError("invalid summary counts")
                if unfinished["needs_review"] > unfinished["total"] or unfinished["review_after_hours"] != 24 or document["unfinished_ingestion"].get("scope") != "all_history":
                    raise ValueError("invalid unfinished run counts")
                counts = {"ingestion_runs_24h": runs, "records": records, "unfinished_ingestion": {**unfinished, "scope": "all_history"}}
        except OpsApiError as exc:
            result.update(status="failed", code=exc.code)
            if exc.http_status is not None:
                result["http_status"] = exc.http_status
        except ValueError:
            result.update(status="failed", code="contract_mismatch")
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        checks.append(result)
    warnings = []
    if counts.get("ingestion_runs_24h", {}).get("stopped", 0):
        warnings.append("stopped_runs_in_24h")
    if counts.get("unfinished_ingestion", {}).get("needs_review", 0):
        warnings.append("unfinished_runs_need_review")
    failed = any(item["status"] == "failed" for item in checks)
    return {
        "report_version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_api_one_shot",
        "status": "failed" if failed else "warning" if warnings else "checks_passed",
        "exit_code": 2 if failed else 1 if warnings else 0,
        "checks": checks,
        "warnings": warnings,
        "observations": counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        # Reserve before requests: never overwrite an old report, even on failure.
        if args.report:
            with args.report.open("x", encoding="utf-8") as output:
                report = check_once(port=args.port, timeout_seconds=args.timeout)
                rendered = json.dumps(report, ensure_ascii=False, indent=2)
                output.write(rendered + "\n")
        else:
            report = check_once(port=args.port, timeout_seconds=args.timeout)
            rendered = json.dumps(report, ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        print(json.dumps({"status": "failed", "code": "configuration_or_report_error", "exit_code": 3}))
        return 3
    print(rendered)
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
