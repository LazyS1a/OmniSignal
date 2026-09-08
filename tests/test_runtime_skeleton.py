from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from pydantic import ValidationError

from omnisignal.api import create_app
from omnisignal.config import Environment, Settings
from omnisignal.secrets import MissingSecretError, resolve_secret
from omnisignal.storage import create_database_engine, upgrade_database


def test_settings_fail_closed_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNISIGNAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("OMNISIGNAL_DB_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="startup stopped safely"):
        Settings.from_env()


def test_production_rejects_test_database() -> None:
    with pytest.raises(ValidationError, match="production requires postgresql"):
        Settings(environment=Environment.PRODUCTION, database_url="sqlite+pysqlite:///:memory:")


def test_environment_database_fields_build_an_encoded_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNISIGNAL_DATABASE_URL", raising=False)
    monkeypatch.setenv("OMNISIGNAL_DB_PASSWORD", "contains:@/characters")
    monkeypatch.setenv("OMNISIGNAL_ENV", "production")
    settings = Settings.from_env()
    assert settings.safe_database_target() == "postgresql+psycopg://db/omnisignal"
    assert "contains%3A%40%2Fcharacters" in settings.database_url


def test_settings_repr_redacts_database_credentials() -> None:
    settings = Settings(
        environment=Environment.TEST,
        database_url="postgresql+psycopg://service-user:do-not-print@database/omnisignal",
    )
    rendered = repr(settings)
    assert "do-not-print" not in rendered
    assert "service-user" not in rendered
    assert "postgresql+psycopg://database/omnisignal" in rendered


def test_sqlite_safe_target_does_not_expose_absolute_path(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "ops.sqlite3"
    settings = Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
    )

    assert settings.safe_database_target() == "sqlite+pysqlite://local/ops.sqlite3"
    assert str(tmp_path) not in repr(settings)


def test_secret_resolver_uses_references_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIXTURE_API_TOKEN", "runtime-only-value")
    assert resolve_secret("env/FIXTURE_API_TOKEN") == "runtime-only-value"

    monkeypatch.delenv("FIXTURE_API_TOKEN")
    with pytest.raises(MissingSecretError, match="unavailable"):
        resolve_secret("env/FIXTURE_API_TOKEN")
    with pytest.raises(MissingSecretError, match="only env"):
        resolve_secret("literal/runtime-only-value")


def test_liveness_and_readiness_are_separate(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'ready.db').as_posix()}"
    upgrade_database(database_url)
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        settings = Settings(environment=Environment.TEST, database_url=database_url)
        app = create_app(settings=settings)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                live = await client.get("/health/live", headers={"x-request-id": "health-test"})
                ready = await client.get("/health/ready")
        return live, ready

    live, ready = asyncio.run(scenario())

    assert live.status_code == 200
    assert live.json() == {"status": "alive", "service": "omnisignal-api"}
    assert live.headers["x-request-id"] == "health-test"
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


class FailingEngine:
    def connect(self) -> None:
        raise ConnectionError("simulated database outage")

    def dispose(self) -> None:
        pass


def test_readiness_fails_without_leaking_database_error() -> None:
    async def scenario() -> httpx.Response:
        settings = Settings(environment=Environment.TEST, database_url="sqlite+pysqlite:///:memory:")
        app = create_app(settings=settings, engine=FailingEngine())  # type: ignore[arg-type]
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get("/health/ready")

    response = asyncio.run(scenario())

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "dependency": "database"}
    assert "simulated database outage" not in response.text


def test_initial_migration_up_down_and_reapply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "migration.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("OMNISIGNAL_DATABASE_URL", database_url)

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    assert set(inspect(engine).get_table_names()) >= {
        "alembic_version",
        "audit_events",
        "connector_states",
        "ingestion_runs",
        "normalization_runs",
        "normalized_records",
        "normalization_run_memberships",
        "plugin_runs",
        "plugin_outputs",
        "source_control_states",
        "source_control_commands",
        "collection_jobs",
    }

    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}

    command.upgrade(config, "head")
    assert "connector_states" in inspect(engine).get_table_names()
    engine.dispose()


def test_known_unversioned_sqlite_is_backed_up_and_adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "known-legacy.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.delenv("OMNISIGNAL_DATABASE_URL", raising=False)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0002_ingested_records")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()

    outcome = upgrade_database(database_url)

    assert outcome.adopted_legacy is True
    assert outcome.backup_path is not None
    assert outcome.backup_path.parent == database_path.parent
    assert outcome.backup_path.is_file()
    engine = create_database_engine(database_url)
    schema = inspect(engine)
    assert "normalization_runs" in schema.get_table_names()
    assert "raw_archive_sha256" in {column["name"] for column in schema.get_columns("ingested_records")}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0006_collection_jobs"
    engine.dispose()


def test_unknown_unversioned_sqlite_is_refused_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "unknown-legacy.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.delenv("OMNISIGNAL_DATABASE_URL", raising=False)
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0002_ingested_records")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("CREATE TABLE unexpected_table (id INTEGER PRIMARY KEY)"))
    engine.dispose()

    with pytest.raises(RuntimeError, match="table set does not match"):
        upgrade_database(database_url)

    assert not tuple(tmp_path.glob("unknown-legacy.pre-0003-*.sqlite3"))
    engine = create_database_engine(database_url)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()
