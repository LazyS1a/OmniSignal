"""Run one deterministic normalization snapshot against the configured database."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.orm import Session

from omnisignal.connectors.github_cli import _database_configuration
from omnisignal.storage import create_database_engine, upgrade_database

from .contracts import load_normalization_config
from .lineage import backfill_archive_lineage
from .repository import run_normalization


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "normalization.yaml"
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "raw"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay active ingested records into the normalized layer")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_normalization_config(args.config.resolve())
        database_url, database_target, _ = _database_configuration()
        upgrade_database(database_url)
        engine = create_database_engine(database_url)
        try:
            with Session(engine) as session:
                lineage = backfill_archive_lineage(session, args.archive_root.resolve())
                summary = run_normalization(session, config)
        finally:
            engine.dispose()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "stopped_safely", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "completed",
                "database_target": database_target,
                "lineage_backfill": asdict(lineage),
                **asdict(summary),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
