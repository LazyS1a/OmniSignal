"""One-shot collection of public search signals using the shared durable runner."""
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
from .public_search_policy import PublicSearchPolicy
from .public_search_signals import PublicSearchSignalsConnector

ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect anonymous relative search interest and suggestions")
    parser.add_argument("--spec", type=Path, default=ROOT / "examples/connectors/public_search_signals.yaml")
    parser.add_argument("--policy", type=Path, default=ROOT / "examples/policies/public_search_signals.yaml")
    parser.add_argument("--registry", type=Path, default=ROOT / "governance/source_registry.yaml")
    parser.add_argument("--workspace", type=Path, default=ROOT / "data/workers/public_search_signals")
    parser.add_argument("--archive-root", type=Path, default=ROOT / "data/raw/public_search_signals")
    parser.add_argument("--sqlite", type=Path, help="Optional separate SQLite database for a local trial")
    args = parser.parse_args()
    try:
        spec = ConnectorSpec.model_validate(yaml.safe_load(args.spec.read_text(encoding="utf-8")))
        policy = PublicSearchPolicy.model_validate(yaml.safe_load(args.policy.read_text(encoding="utf-8")))
        approval = load_source_approval(args.registry, spec)
        if args.sqlite:
            path = args.sqlite.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            database_url = URL.create("sqlite+pysqlite", database=str(path)).render_as_string(hide_password=False)
            target, initialize = f"sqlite+pysqlite://local/{path.name}", True
        else:
            database_url, target, initialize = _database_configuration()
        connector = PublicSearchSignalsConnector(spec, policy, approval, workspace=args.workspace,
                                                archive=FileRawResponseArchive(args.archive_root))
        summary = asyncio.run(run_connector_durably(connector, database_url=database_url,
                            database_target=target, initialize_schema=initialize))
    except ConnectorFailure as exc:
        print(json.dumps({"status": "stopped_safely", "category": exc.category.value,
                          "retry_after_seconds": exc.retry_after_seconds}))
        return 2
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "invalid_configuration"}))
        return 2
    print(json.dumps({"status": "completed", "quality": "degraded" if connector.last_warnings else "healthy",
                      "warnings": list(connector.last_warnings), **asdict(summary)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
