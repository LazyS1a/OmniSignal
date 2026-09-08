from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from omnisignal.api import create_app
from omnisignal.config import Environment, Settings
from omnisignal.storage import create_database_engine, upgrade_database
from omnisignal.storage.backup import create_snapshot, restore_snapshot, verify_snapshot


def _url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    source = tmp_path / "数据 # test.db"
    upgrade_database(_url(source))
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO connector_states(source_id, checkpoint, checkpoint_version, updated_at) VALUES (?, ?, ?, ?)",
            ("fixture", '{"cursor":"private-fixture-content"}', 2, "2026-09-05 00:00:00"),
        )
    return source


@pytest.mark.parametrize("damage", ["empty", "old_revision", "missing_table", "missing_column"])
def test_ready_fails_closed_for_incompatible_schema(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "schema.db"
    if damage != "empty":
        upgrade_database(_url(path))
    engine = create_database_engine(_url(path))
    with engine.begin() as connection:
        if damage == "old_revision":
            connection.execute(text("UPDATE alembic_version SET version_num='0004_processing_plugins'"))
        elif damage == "missing_table":
            connection.execute(text("DROP TABLE connector_states"))
        elif damage == "missing_column":
            connection.execute(text("ALTER TABLE connector_states RENAME COLUMN checkpoint TO wrong_column"))
    app = create_app(settings=Settings(environment=Environment.TEST, database_url=_url(path)), engine=engine)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "dependency": "schema"}
        assert "SELECT" not in response.text and str(tmp_path) not in response.text


def test_same_api_recovers_after_missing_schema_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "recover.db"
    app = create_app(settings=Settings(environment=Environment.TEST, database_url=_url(path)))
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 503
        response = client.get("/ops/summary")
        assert response.status_code == 503
        assert response.json() == {"detail": "Operational data is temporarily unavailable."}
        upgrade_database(_url(path))
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/ops/summary").status_code == 200


def test_backup_and_restore_preserve_content_without_touching_source(database: Path, tmp_path: Path) -> None:
    original = database.read_bytes()
    snapshot = tmp_path / "snapshot.db"
    backup = create_snapshot(database, snapshot)
    assert backup.database_only is True
    assert backup.table_counts["connector_states"] == 1
    assert backup.file_sha256 == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    restored_path = tmp_path / "recovered.db"
    restored = restore_snapshot(snapshot, restored_path, expected_sha256=backup.file_sha256)
    assert restored.content_sha256 == backup.content_sha256
    assert restored.table_counts == backup.table_counts
    assert database.read_bytes() == original
    app = create_app(settings=Settings(environment=Environment.TEST, database_url=_url(restored_path)))
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200


def test_online_backup_includes_committed_wal(database: Path, tmp_path: Path) -> None:
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE connector_states SET checkpoint_version=9")
        writer.commit()
        destination = tmp_path / "wal-snapshot.db"
        create_snapshot(database, destination)
        with sqlite3.connect(destination) as restored:
            assert restored.execute("SELECT checkpoint_version FROM connector_states").fetchone() == (9,)


@pytest.mark.parametrize("same_source", [False, True])
def test_backup_never_overwrites(database: Path, tmp_path: Path, same_source: bool) -> None:
    destination = database if same_source else tmp_path / "existing.db"
    if not same_source:
        destination.write_bytes(b"existing unrelated data")
    original = destination.read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        create_snapshot(database, destination)
    assert destination.read_bytes() == original


def test_tampered_backup_refused_before_creating_restore(database: Path, tmp_path: Path) -> None:
    backup_path = tmp_path / "backup.db"
    evidence = create_snapshot(database, backup_path)
    with sqlite3.connect(backup_path) as connection:
        connection.execute("UPDATE connector_states SET checkpoint_version=99")
    destination = tmp_path / "refused.db"
    with pytest.raises(ValueError, match="digest mismatch"):
        restore_snapshot(backup_path, destination, expected_sha256=evidence.file_sha256)
    assert not destination.exists()


def test_missing_source_is_not_created(tmp_path: Path) -> None:
    source, destination = tmp_path / "missing.db", tmp_path / "unused.db"
    with pytest.raises(FileNotFoundError):
        create_snapshot(source, destination)
    assert not source.exists() and not destination.exists()


def test_wrong_schema_backup_is_not_accepted(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(id INTEGER)")
    with pytest.raises(RuntimeError, match="revision mismatch"):
        verify_snapshot(path)


def test_verification_refuses_live_wal_not_covered_by_file_digest(database: Path) -> None:
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE connector_states SET checkpoint_version=20")
        writer.commit()
        with pytest.raises(ValueError, match="standalone snapshot"):
            verify_snapshot(database)


def test_migration_entrypoint_works_outside_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "from-vault.db"
    upgrade_database(_url(path))
    assert verify_snapshot(path).revision == ("0006_collection_jobs",)


def test_explicit_historical_revision_allows_verified_pre_migration_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "pre-migration.db"
    monkeypatch.setenv("OMNISIGNAL_DATABASE_URL", _url(source))
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "0005_source_control")
    backup = tmp_path / "pre-migration-backup.db"
    restored = tmp_path / "pre-migration-restored.db"

    evidence = create_snapshot(source, backup, expected_revision="0005_source_control")
    restored_evidence = restore_snapshot(
        backup,
        restored,
        expected_sha256=evidence.file_sha256,
        expected_revision="0005_source_control",
    )

    assert evidence.revision == restored_evidence.revision == ("0005_source_control",)
    assert evidence.content_sha256 == restored_evidence.content_sha256


def test_restore_refuses_existing_file(database: Path, tmp_path: Path) -> None:
    snapshot = tmp_path / "backup.db"
    evidence = create_snapshot(database, snapshot)
    original = database.read_bytes()
    with pytest.raises(FileExistsError):
        restore_snapshot(snapshot, database, expected_sha256=evidence.file_sha256)
    assert database.read_bytes() == original


def test_backup_refuses_existing_sidecar(database: Path, tmp_path: Path) -> None:
    destination = tmp_path / "new.db"
    sidecar = tmp_path / "new.db-wal"
    sidecar.write_bytes(b"unrelated sidecar")
    with pytest.raises(ValueError, match="sidecars"):
        create_snapshot(database, destination)
    assert not destination.exists()
    assert sidecar.read_bytes() == b"unrelated sidecar"


def test_backup_timeout_does_not_produce_verified_evidence(database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omnisignal.storage import backup

    ticks = iter((0.0, 100.0))
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        create_snapshot(database, tmp_path / "incomplete.db", timeout_seconds=1)
    assert database.is_file()
