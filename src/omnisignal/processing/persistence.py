"""Durable, append-only storage for isolated plugin attempts and their outputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from omnisignal.storage.models import PluginOutput, PluginRun

if TYPE_CHECKING:
    from .runner import PluginExecution


def persist_plugin_execution(session: Session, execution: "PluginExecution") -> None:
    if session.get(PluginRun, execution.run_id) is not None:
        raise RuntimeError("plugin run id already exists")
    session.add(
        PluginRun(
            run_id=execution.run_id,
            execution_id=execution.execution_id,
            plugin_id=execution.plugin_id,
            plugin_version=execution.plugin_version,
            manifest_hash=execution.manifest_hash,
            output_schema_version=execution.output_schema_version,
            status=execution.status,
            records_seen=execution.records_seen,
            records_sent=execution.records_sent,
            records_skipped=execution.records_skipped,
            output_count=execution.output_count,
            output_hash=execution.output_hash,
            error_code=execution.error_code,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
        )
    )
    for output in execution.outputs:
        session.add(
            PluginOutput(
                run_id=execution.run_id,
                normalized_id=output.normalized_id,
                values=output.values,
                quality_status=output.quality_status,
                quality_codes=list(output.quality_codes),
            )
        )
    session.commit()
