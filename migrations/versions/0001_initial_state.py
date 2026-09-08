"""Create connector state, ingestion run and audit event tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_states",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "PAUSED",
                "QUARANTINED",
                name="runstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("records_seen", sa.Integer(), nullable=False),
        sa.Column("records_written", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("source_id", "idempotency_key", name="uq_run_source_idempotency"),
    )
    op.create_index("ix_ingestion_runs_source_id", "ingestion_runs", ["source_id"], unique=False)
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.run_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_audit_events_run_id", "audit_events", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_events_run_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_ingestion_runs_source_id", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_table("connector_states")
