"""SQLite online snapshots and verified restore to a NEW file only.

Database-only: raw archives, configuration and external secrets are not included.
Failed outputs are retained for diagnosis and must never be treated as backups.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from .readiness import assert_schema_ready, expected_heads


@dataclass(frozen=True)
class SnapshotEvidence:
    verified_at: str
    database_only: bool
    revision: tuple[str, ...]
    file_sha256: str
    content_sha256: str
    table_counts: dict[str, int]


def _readonly(path: Path) -> sqlite3.Connection:
    # URI quoting also handles spaces, Unicode, # and ? in local file names.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _assert_standalone(path: Path) -> None:
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("verification requires a standalone snapshot, not a live WAL database")


def verify_snapshot(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_revision: str | None = None,
) -> SnapshotEvidence:
    path = path.resolve(strict=True)
    _assert_standalone(path)
    file_hash = _file_hash(path)
    if expected_sha256 is not None and file_hash != expected_sha256:
        raise ValueError("backup digest mismatch; restore refused")
    engine = create_engine("sqlite://", creator=lambda: _readonly(path), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            if expected_revision is None:
                assert_schema_ready(connection, expected_heads())
            elif frozenset(MigrationContext.configure(connection).get_current_heads()) != {expected_revision}:
                raise RuntimeError("database migration revision mismatch")
    finally:
        engine.dispose()
    with closing(_readonly(path)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("backup integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("backup foreign key check failed")
        counts = {}
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (name,) in names:
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        digest = hashlib.sha256()
        # Streaming SQL dump covers schema AND values (not just record counts).
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        revision = tuple(row[0] for row in connection.execute("SELECT version_num FROM alembic_version ORDER BY version_num"))
    _assert_standalone(path)
    if _file_hash(path) != file_hash:
        raise ValueError("snapshot changed during verification")
    return SnapshotEvidence(
        verified_at=datetime.now(timezone.utc).isoformat(),
        database_only=True,
        revision=revision,
        file_sha256=file_hash,
        content_sha256=digest.hexdigest(),
        table_counts=counts,
    )


def _copy_new(source: Path, destination: Path, timeout_seconds: float) -> None:
    if not 0 < timeout_seconds <= 3600:
        raise ValueError("backup timeout must be between 0 and 3600 seconds")
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("source and destination must differ")
    if any(destination.with_name(destination.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("destination has existing SQLite sidecars; use a new path")
    # Exclusive reservation refuses existing files, symlinks and source hardlinks.
    # Do not create parent directories or delete/overwrite another file.
    with destination.open("xb"):
        pass
    deadline = time.monotonic() + timeout_seconds

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("SQLite online backup exceeded its time budget")

    with closing(_readonly(source)) as origin, closing(sqlite3.connect(destination)) as target:
        origin.backup(target, pages=256, progress=progress, sleep=0.05)


def create_snapshot(
    source: Path,
    destination: Path,
    *,
    timeout_seconds: float = 30,
    expected_revision: str | None = None,
) -> SnapshotEvidence:
    _copy_new(source, destination, timeout_seconds)
    return verify_snapshot(destination, expected_revision=expected_revision)


def restore_snapshot(
    backup: Path,
    destination: Path,
    *,
    expected_sha256: str,
    timeout_seconds: float = 30,
    expected_revision: str | None = None,
) -> SnapshotEvidence:
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        raise ValueError("a lowercase SHA-256 from backup evidence is required")
    before = verify_snapshot(backup, expected_sha256=expected_sha256, expected_revision=expected_revision)
    _copy_new(backup, destination, timeout_seconds)
    restored = verify_snapshot(destination, expected_revision=expected_revision)
    if restored.content_sha256 != before.content_sha256 or _file_hash(backup) != before.file_sha256:
        raise ValueError("restore content mismatch; do not use the destination")
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "verify", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--revision", help="Exact pre-migration Alembic revision expected in this snapshot")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.action != "verify" and args.destination is None:
        parser.error("--destination is required for backup/restore")
    if args.action == "restore" and args.sha256 is None:
        parser.error("--sha256 from backup evidence is required for restore")
    try:
        if args.action == "backup":
            result = create_snapshot(args.source, args.destination, timeout_seconds=args.timeout,
                                     expected_revision=args.revision)
        elif args.action == "restore":
            result = restore_snapshot(args.source, args.destination, expected_sha256=args.sha256,
                                      timeout_seconds=args.timeout, expected_revision=args.revision)
        else:
            result = verify_snapshot(args.source, expected_sha256=args.sha256,
                                     expected_revision=args.revision)
    except Exception as exc:
        # Never echo a database error, SQL values, credentials or private paths.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "destination_usable": False}))
        return 1
    print(json.dumps({"status": "verified", **asdict(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
