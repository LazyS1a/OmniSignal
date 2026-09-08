"""Add persisted operator-triggered collection jobs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_collection_jobs"
down_revision = "0005_source_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("task_definition_hash", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("command_hash"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_collection_jobs_source_id", "collection_jobs", ["source_id"], unique=False)
    op.create_index("ix_collection_jobs_status", "collection_jobs", ["status"], unique=False)
    op.create_index("ix_collection_jobs_task_id", "collection_jobs", ["task_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_collection_jobs_task_id", table_name="collection_jobs")
    op.drop_index("ix_collection_jobs_status", table_name="collection_jobs")
    op.drop_index("ix_collection_jobs_source_id", table_name="collection_jobs")
    op.drop_table("collection_jobs")
