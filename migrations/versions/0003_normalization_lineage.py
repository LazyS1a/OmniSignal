"""Add archive lineage and deterministic normalization tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_normalization_lineage"
down_revision = "0002_ingested_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingested_records", sa.Column("raw_archive_sha256", sa.String(length=64), nullable=True))
    op.create_table(
        "normalization_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("normalizer_version", sa.String(length=32), nullable=False),
        sa.Column("input_set_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("normalized_count", sa.Integer(), nullable=False),
        sa.Column("unconfigured_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_group_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "normalized_records",
        sa.Column("normalized_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=256), nullable=False),
        sa.Column("input_raw_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_archive_sha256", sa.String(length=64), nullable=True),
        sa.Column("permission", sa.String(length=128), nullable=False),
        sa.Column("source_schema_version", sa.String(length=16), nullable=False),
        sa.Column("normalizer_version", sa.String(length=32), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("entity_ids", sa.JSON(), nullable=False),
        sa.Column("exact_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("simhash64", sa.String(length=16), nullable=True),
        sa.Column("parent_source_record_id", sa.String(length=256), nullable=True),
        sa.Column("quality_status", sa.String(length=20), nullable=False),
        sa.Column("quality_codes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("normalized_id"),
        sa.UniqueConstraint(
            "source_id",
            "source_record_id",
            "input_raw_hash",
            "normalizer_version",
            "config_hash",
            name="uq_normalized_input_version",
        ),
    )
    op.create_index("ix_normalized_records_source_id", "normalized_records", ["source_id"], unique=False)
    op.create_table(
        "quality_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("normalization_run_id", sa.String(length=64), nullable=False),
        sa.Column("normalized_id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("field", sa.String(length=80), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["normalization_run_id"], ["normalization_runs.run_id"]),
        sa.ForeignKeyConstraint(["normalized_id"], ["normalized_records.normalized_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_quality_events_normalization_run_id", "quality_events", ["normalization_run_id"], unique=False)
    op.create_index("ix_quality_events_normalized_id", "quality_events", ["normalized_id"], unique=False)
    op.create_table(
        "context_edges",
        sa.Column("edge_id", sa.String(length=64), nullable=False),
        sa.Column("normalization_run_id", sa.String(length=64), nullable=False),
        sa.Column("from_normalized_id", sa.String(length=64), nullable=False),
        sa.Column("to_normalized_id", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["normalization_run_id"], ["normalization_runs.run_id"]),
        sa.ForeignKeyConstraint(["from_normalized_id"], ["normalized_records.normalized_id"]),
        sa.ForeignKeyConstraint(["to_normalized_id"], ["normalized_records.normalized_id"]),
        sa.PrimaryKeyConstraint("edge_id"),
    )
    op.create_index("ix_context_edges_normalization_run_id", "context_edges", ["normalization_run_id"], unique=False)
    op.create_index("ix_context_edges_from_normalized_id", "context_edges", ["from_normalized_id"], unique=False)
    op.create_index("ix_context_edges_to_normalized_id", "context_edges", ["to_normalized_id"], unique=False)
    op.create_table(
        "duplicate_groups",
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("normalization_run_id", sa.String(length=64), nullable=False),
        sa.Column("duplicate_kind", sa.String(length=20), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["normalization_run_id"], ["normalization_runs.run_id"]),
        sa.PrimaryKeyConstraint("group_id"),
    )
    op.create_index("ix_duplicate_groups_normalization_run_id", "duplicate_groups", ["normalization_run_id"], unique=False)
    op.create_table(
        "duplicate_memberships",
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("normalized_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["duplicate_groups.group_id"]),
        sa.ForeignKeyConstraint(["normalized_id"], ["normalized_records.normalized_id"]),
        sa.PrimaryKeyConstraint("group_id", "normalized_id"),
    )


def downgrade() -> None:
    op.drop_table("duplicate_memberships")
    op.drop_index("ix_duplicate_groups_normalization_run_id", table_name="duplicate_groups")
    op.drop_table("duplicate_groups")
    op.drop_index("ix_context_edges_to_normalized_id", table_name="context_edges")
    op.drop_index("ix_context_edges_from_normalized_id", table_name="context_edges")
    op.drop_index("ix_context_edges_normalization_run_id", table_name="context_edges")
    op.drop_table("context_edges")
    op.drop_index("ix_quality_events_normalized_id", table_name="quality_events")
    op.drop_index("ix_quality_events_normalization_run_id", table_name="quality_events")
    op.drop_table("quality_events")
    op.drop_index("ix_normalized_records_source_id", table_name="normalized_records")
    op.drop_table("normalized_records")
    op.drop_table("normalization_runs")
    with op.batch_alter_table("ingested_records") as batch_op:
        batch_op.drop_column("raw_archive_sha256")
