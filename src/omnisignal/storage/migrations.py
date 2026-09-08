"""Programmatic Alembic upgrade with fail-closed legacy SQLite adoption."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from .database import create_database_engine


PROJECT_ROOT = Path(__file__).resolve().parents[3]

_LEGACY_REVISION = "0002_ingested_records"
_LEGACY_COLUMNS = {
    "audit_events": ("event_id", "run_id", "actor", "action", "target", "detail", "created_at"),
    "connector_states": ("source_id", "checkpoint", "checkpoint_version", "updated_at"),
    "ingested_records": (
        "source_id",
        "source_record_id",
        "payload",
        "raw_hash",
        "schema_version",
        "permission",
        "first_seen_at",
        "last_seen_at",
        "deleted_at",
    ),
    "ingestion_runs": (
        "run_id",
        "source_id",
        "idempotency_key",
        "status",
        "records_seen",
        "records_written",
        "error_code",
        "error_summary",
        "started_at",
        "finished_at",
        "created_at",
    ),
}
_LEGACY_PRIMARY_KEYS = {
    "audit_events": ("event_id",),
    "connector_states": ("source_id",),
    "ingested_records": ("source_id", "source_record_id"),
    "ingestion_runs": ("run_id",),
}
_LEGACY_UNIQUES = {
    "ingested_records": {("source_id", "source_record_id")},
    "ingestion_runs": {("source_id", "idempotency_key")},
}
_LEGACY_INDEXES = {
    "audit_events": {("run_id",)},
    "ingestion_runs": {("source_id",)},
}


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    adopted_legacy: bool
    backup_path: Path | None


def upgrade_database(database_url: str) -> MigrationOutcome:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations").replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    engine = create_database_engine(database_url)
    try:
        schema = inspect(engine)
        tables = {name for name in schema.get_table_names() if not name.startswith("sqlite_")}
        if not tables or "alembic_version" in tables:
            command.upgrade(config, "head")
            return MigrationOutcome(adopted_legacy=False, backup_path=None)
        if engine.dialect.name != "sqlite":
            raise RuntimeError("unversioned non-SQLite database refused; manual migration review required")
        _validate_legacy_schema(schema, tables)
    finally:
        engine.dispose()

    database_path = _sqlite_database_path(database_url)
    backup_path = _backup_sqlite_database(database_path)
    command.stamp(config, _LEGACY_REVISION)
    command.upgrade(config, "head")
    return MigrationOutcome(adopted_legacy=True, backup_path=backup_path)


def _validate_legacy_schema(schema: object, tables: set[str]) -> None:
    expected_tables = set(_LEGACY_COLUMNS)
    if tables != expected_tables:
        raise RuntimeError("unversioned SQLite schema refused; table set does not match the known legacy schema")
    for table, expected_columns in _LEGACY_COLUMNS.items():
        columns = tuple(column["name"] for column in schema.get_columns(table))  # type: ignore[attr-defined]
        primary_key = tuple(schema.get_pk_constraint(table)["constrained_columns"])  # type: ignore[attr-defined]
        uniques = {
            tuple(constraint["column_names"])
            for constraint in schema.get_unique_constraints(table)  # type: ignore[attr-defined]
        }
        indexes = {
            tuple(index["column_names"])
            for index in schema.get_indexes(table)  # type: ignore[attr-defined]
            if not index.get("unique")
        }
        if columns != expected_columns or primary_key != _LEGACY_PRIMARY_KEYS[table]:
            raise RuntimeError(f"unversioned SQLite schema refused; {table} columns or primary key differ")
        if not _LEGACY_UNIQUES.get(table, set()).issubset(uniques):
            raise RuntimeError(f"unversioned SQLite schema refused; {table} unique constraint differs")
        if not _LEGACY_INDEXES.get(table, set()).issubset(indexes):
            raise RuntimeError(f"unversioned SQLite schema refused; {table} index differs")
    foreign_keys = schema.get_foreign_keys("audit_events")  # type: ignore[attr-defined]
    links = {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in foreign_keys
    }
    if (("run_id",), "ingestion_runs", ("run_id",)) not in links:
        raise RuntimeError("unversioned SQLite schema refused; audit event foreign key differs")


def _sqlite_database_path(database_url: str) -> Path:
    database = make_url(database_url).database
    if database in {None, "", ":memory:"}:
        raise RuntimeError("legacy adoption requires a file-backed SQLite database")
    return Path(database).expanduser().resolve()


def _backup_sqlite_database(database_path: Path) -> Path:
    if not database_path.is_file():
        raise RuntimeError("legacy SQLite database file is unavailable for backup")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.with_name(f"{database_path.stem}.pre-0003-{timestamp}.sqlite3")
    with sqlite3.connect(database_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError("legacy SQLite backup failed integrity verification")
    return backup_path
