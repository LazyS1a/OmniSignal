"""Command-line entry point for approved static-web source policies."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import yaml

from omnisignal.contracts import ConnectorFailure, ConnectorSpec
from omnisignal.governance import load_source_approval

from .archive import FileRawResponseArchive
from .durable import run_connector_durably
from .github_cli import _database_configuration
from .static_web import StaticWebConnector
from .static_web_policy import StaticWebPolicy


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SPEC = PROJECT_ROOT / "examples" / "connectors" / "static_html.yaml"
DEFAULT_POLICY = PROJECT_ROOT / "examples" / "policies" / "static_html.yaml"
DEFAULT_WORKSPACE = PROJECT_ROOT / "data" / "workers" / "fixture_static_html"
DEFAULT_ARCHIVE = PROJECT_ROOT / "data" / "raw" / "fixture_static_html"
DEFAULT_REGISTRY = PROJECT_ROOT / "governance" / "source_registry.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one approved static-web connector cycle")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        spec = ConnectorSpec.model_validate(yaml.safe_load(args.spec.read_text(encoding="utf-8")))
        policy = StaticWebPolicy.model_validate(yaml.safe_load(args.policy.read_text(encoding="utf-8")))
        approval = load_source_approval(args.registry, spec)
        connector = StaticWebConnector(
            spec,
            policy,
            approval,
            workspace=args.workspace,
            archive=FileRawResponseArchive(args.archive_root),
        )
        database_url, database_target, initialize_schema = _database_configuration()
        summary = asyncio.run(
            run_connector_durably(
                connector,
                database_url=database_url,
                database_target=database_target,
                initialize_schema=initialize_schema,
            )
        )
    except ConnectorFailure as exc:
        print(
            json.dumps(
                {
                    "status": "stopped_safely",
                    "category": exc.category.value,
                    "action": exc.action.value,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "invalid_configuration", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "completed", **asdict(summary)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
