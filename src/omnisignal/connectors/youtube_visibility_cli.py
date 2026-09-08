"""Run a visibility snapshot and print its counters and evidence."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import yaml
from sqlalchemy.engine import URL

from omnisignal.contracts import ConnectorFailure, ConnectorSpec
from omnisignal.governance import load_source_approval
from .archive import FileRawResponseArchive
from .durable import run_connector_durably
from .github_cli import _database_configuration
from .public_search_worker import _write_result
from .youtube_visibility import YouTubeVisibilityConnector
from .youtube_visibility_policy import YouTubeVisibilityPolicy

ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect one YouTube video search visibility sample")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--output", type=Path, help="Optional JSON report; database remains the durable copy")
    args = parser.parse_args()
    try:
        policy = YouTubeVisibilityPolicy.model_validate(yaml.safe_load(args.policy.read_text(encoding="utf-8")))
        spec = ConnectorSpec.model_validate(yaml.safe_load((ROOT / "examples/connectors/youtube_visibility.yaml").read_text(encoding="utf-8")))
        approval = load_source_approval(ROOT / "governance/source_registry.yaml", spec)
        if args.sqlite:
            path = args.sqlite.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            url = URL.create("sqlite+pysqlite", database=str(path)).render_as_string(hide_password=False)
            target, initialize = f"sqlite+pysqlite://local/{path.name}", True
        else:
            url, target, initialize = _database_configuration()
        connector = YouTubeVisibilityConnector(spec, policy, approval, workspace=ROOT / "data/workers/youtube_visibility",
                                               archive=FileRawResponseArchive(ROOT / "data/raw/youtube_visibility"))
        summary = asyncio.run(run_connector_durably(connector, database_url=url, database_target=target,
                                                    initialize_schema=initialize))
    except ConnectorFailure as exc:
        print(json.dumps({"status": "stopped_safely", "category": exc.category.value}))
        return 2
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "invalid_configuration"}))
        return 2
    result = {"status": "completed", "run": asdict(summary), "report": connector.last_report}
    if args.output:
        if args.output.exists():
            result["export_status"] = "skipped_existing_file"
        else:
            try:
                _write_result(args.output, result)
            except OSError:
                result["export_status"] = "failed_database_copy_preserved"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
