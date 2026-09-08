"""Add exact normalization membership and durable plugin attempt history."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_ops_read_model"
down_revision = "0003_normalization_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "normalization_run_memberships",
        sa.Column("normalization_run_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=256), nullable=False),
        sa.Column("input_raw_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_id", sa.String(length=64), nullable=True),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["normalization_run_id"], ["normalization_runs.run_id"]),
        sa.ForeignKeyConstraint(["normalized_id"], ["normalized_records.normalized_id"]),
        sa.PrimaryKeyConstraint("normalization_run_id", "source_id", "source_record_id"),
    )
    op.create_index(
        "ix_normalization_run_memberships_normalized_id",
        "normalization_run_memberships",
        ["normalized_id"],
        unique=False,
    )
    op.create_table(
        "plugin_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_version", sa.String(length=32), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("output_schema_version", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("records_seen", sa.Integer(), nullable=False),
        sa.Column("records_sent", sa.Integer(), nullable=False),
        sa.Column("records_skipped", sa.Integer(), nullable=False),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_plugin_runs_execution_id", "plugin_runs", ["execution_id"], unique=False)
    op.create_index("ix_plugin_runs_plugin_id", "plugin_runs", ["plugin_id"], unique=False)
    op.create_index("ix_plugin_runs_status", "plugin_runs", ["status"], unique=False)
    op.create_table(
        "plugin_outputs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("normalized_id", sa.String(length=64), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("quality_status", sa.String(length=20), nullable=False),
        sa.Column("quality_codes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["plugin_runs.run_id"]),
        sa.ForeignKeyConstraint(["normalized_id"], ["normalized_records.normalized_id"]),
        sa.PrimaryKeyConstraint("run_id", "normalized_id"),
    )
    op.create_index("ix_plugin_outputs_normalized_id", "plugin_outputs", ["normalized_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_plugin_outputs_normalized_id", table_name="plugin_outputs")
    op.drop_table("plugin_outputs")
    op.drop_index("ix_plugin_runs_status", table_name="plugin_runs")
    op.drop_index("ix_plugin_runs_plugin_id", table_name="plugin_runs")
    op.drop_index("ix_plugin_runs_execution_id", table_name="plugin_runs")
    op.drop_table("plugin_runs")
    op.drop_index(
        "ix_normalization_run_memberships_normalized_id",
        table_name="normalization_run_memberships",
    )
    op.drop_table("normalization_run_memberships")
