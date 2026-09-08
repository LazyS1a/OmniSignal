"""Create idempotent ingested records table."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_ingested_records"
down_revision = "0001_initial_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingested_records",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=256), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("raw_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("permission", sa.String(length=128), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("source_id", "source_record_id"),
        sa.UniqueConstraint("source_id", "source_record_id", name="uq_record_source_identity"),
    )


def downgrade() -> None:
    op.drop_table("ingested_records")
