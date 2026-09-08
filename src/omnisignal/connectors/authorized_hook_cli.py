"""One-shot durable entry point for an approved Hook worker."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import yaml

from omnisignal.contracts import ConnectorFailure, ConnectorSpec
from omnisignal.governance import load_hook_source_approval

from .archive import FileRawResponseArchive
from .authorized_hook import AuthorizedHookConnector
from .authorized_hook_policy import AuthorizedHookPolicy
from .durable import run_connector_durably
from .github_cli import _database_configuration


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SPEC = PROJECT_ROOT / "examples" / "connectors" / "authorized_hook.yaml"
DEFAULT_POLICY = PROJECT_ROOT / "examples" / "policies" / "authorized_hook.yaml"
DEFAULT_REGISTRY = PROJECT_ROOT / "governance" / "source_registry.yaml"
DEFAULT_WORKSPACE = PROJECT_ROOT / "data" / "workers" / "fixture_hook"
DEFAULT_ARCHIVE = PROJECT_ROOT / "data" / "raw" / "fixture_hook"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one approved Hook worker cycle")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--reset-circuit", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        spec = ConnectorSpec.model_validate(yaml.safe_load(args.spec.read_text(encoding="utf-8")))
        policy = AuthorizedHookPolicy.model_validate(yaml.safe_load(args.policy.read_text(encoding="utf-8")))
        approval = load_hook_source_approval(args.registry, spec)
        connector = AuthorizedHookConnector(
            spec,
            policy,
            approval,
            workspace=args.workspace,
            archive=FileRawResponseArchive(args.archive_root),
        )
        if args.reset_circuit:
            connector.reset_circuit()
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
