"""Add durable source controls and idempotent command results."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_source_control"
down_revision = "0004_ops_read_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_control_states",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=240), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_table(
        "source_control_commands",
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("command_hash"),
    )
    op.create_index(
        "ix_source_control_commands_source_id",
        "source_control_commands",
        ["source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_source_control_commands_source_id", table_name="source_control_commands")
    op.drop_table("source_control_commands")
    op.drop_table("source_control_states")
