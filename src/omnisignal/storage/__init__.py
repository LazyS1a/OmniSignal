"""Database engine, models and session helpers."""

from .database import create_database_engine, create_session_factory
from .migrations import MigrationOutcome, upgrade_database
from .models import (
    AuditEvent,
    Base,
    CollectionJob,
    ConnectorState,
    ContextEdge,
    DuplicateGroup,
    DuplicateMembership,
    IngestedRecord,
    IngestionRun,
    NormalizationRun,
    NormalizationRunMembership,
    NormalizedRecord,
    PluginOutput,
    PluginRun,
    QualityEvent,
    RunStatus,
    SourceControlCommand,
    SourceControlState,
)
from .control import assert_source_enabled
from .records import UpsertSummary, mark_records_deleted, upsert_records

__all__ = [
    "AuditEvent",
    "Base",
    "CollectionJob",
    "ConnectorState",
    "ContextEdge",
    "DuplicateGroup",
    "DuplicateMembership",
    "IngestedRecord",
    "IngestionRun",
    "MigrationOutcome",
    "NormalizationRun",
    "NormalizationRunMembership",
    "NormalizedRecord",
    "PluginOutput",
    "PluginRun",
    "QualityEvent",
    "RunStatus",
    "SourceControlCommand",
    "SourceControlState",
    "UpsertSummary",
    "create_database_engine",
    "create_session_factory",
    "mark_records_deleted",
    "upsert_records",
    "upgrade_database",
    "assert_source_enabled",
]
