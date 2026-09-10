"""Environment configuration with fail-closed production defaults."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.engine import URL, make_url


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Environment
    database_url: str = Field(min_length=1)
    log_level: str = "INFO"
    service_name: str = "omnisignal-api"
    service_version: str = "0.2.0"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        url = make_url(value)
        if url.drivername not in {"postgresql+psycopg", "sqlite", "sqlite+pysqlite"}:
            raise ValueError("database driver must be postgresql+psycopg or sqlite for tests")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @model_validator(mode="after")
    def require_postgresql_in_production(self) -> "Settings":
        if self.environment == Environment.PRODUCTION:
            if make_url(self.database_url).drivername != "postgresql+psycopg":
                raise ValueError("production requires postgresql+psycopg")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("OMNISIGNAL_DATABASE_URL")
        if not database_url:
            password = os.getenv("OMNISIGNAL_DB_PASSWORD")
            if not password:
                raise RuntimeError(
                    "OMNISIGNAL_DATABASE_URL or OMNISIGNAL_DB_PASSWORD is required; service startup stopped safely"
                )
            database_url = URL.create(
                "postgresql+psycopg",
                username=os.getenv("OMNISIGNAL_DB_USER", "omnisignal"),
                password=password,
                host=os.getenv("OMNISIGNAL_DB_HOST", "db"),
                port=int(os.getenv("OMNISIGNAL_DB_PORT", "5432")),
                database=os.getenv("OMNISIGNAL_DB_NAME", "omnisignal"),
            ).render_as_string(hide_password=False)
        return cls(
            environment=os.getenv("OMNISIGNAL_ENV", Environment.DEVELOPMENT.value),
            database_url=database_url,
            log_level=os.getenv("OMNISIGNAL_LOG_LEVEL", "INFO"),
            service_version=os.getenv("OMNISIGNAL_VERSION", "0.2.0"),
        )

    def safe_database_target(self) -> str:
        url = make_url(self.database_url)
        host = url.host or "local"
        database = url.database or "memory"
        if url.drivername in {"sqlite", "sqlite+pysqlite"} and database != ":memory:":
            database = Path(database).name
        return f"{url.drivername}://{host}/{database}"

    def __repr_args__(self) -> list[tuple[str | None, object]]:
        return [
            ("environment", self.environment),
            ("database_url", self.safe_database_target()),
            ("log_level", self.log_level),
            ("service_name", self.service_name),
            ("service_version", self.service_version),
        ]
