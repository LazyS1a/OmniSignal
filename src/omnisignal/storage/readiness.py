"""Read-only deployment compatibility checks, never schema repair."""

from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.engine import Connection

from .models import Base


def expected_heads() -> frozenset[str]:
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[3] / "migrations"))
    return frozenset(scripts.get_heads())


def assert_schema_ready(connection: Connection, heads: frozenset[str]) -> None:
    if not heads or frozenset(MigrationContext.configure(connection).get_current_heads()) != heads:
        raise RuntimeError("database migration revision mismatch")
    # A revision stamp alone does not prove that required tables/columns exist.
    # LIMIT 0 checks permissions and columns without fetching any business rows.
    for table in Base.metadata.sorted_tables:
        connection.execute(select(table).limit(0)).close()
