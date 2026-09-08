"""Core-side orchestration for approved, optional processing plugins."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session

from omnisignal.normalization import NormalizedDocument
from omnisignal.runtime import IsolatedWorkerError, run_json_worker

from .contracts import (
    PluginInputRecord,
    PluginJob,
    PluginManifest,
    PluginOutputRecord,
    PluginResult,
    output_value_matches_type,
)
from .protocol import PROTOCOL_VERSION, SCHEMA_FINGERPRINT


@dataclass(frozen=True, slots=True)
class PluginExecution:
    run_id: str
    execution_id: str
    plugin_id: str
    plugin_version: str
    manifest_hash: str
    output_schema_version: str
    status: str
    records_seen: int
    records_sent: int
    records_skipped: int
    output_count: int
    output_hash: str | None
    error_code: str | None
    started_at: datetime
    finished_at: datetime
    outputs: tuple[PluginOutputRecord, ...]


async def run_plugin(
    manifest: PluginManifest,
    documents: tuple[NormalizedDocument, ...],
    *,
    workspace: Path,
    session: Session | None = None,
) -> PluginExecution:
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)
    execution_id = _execution_id(manifest, documents)

    def finish(
        status: str,
        records_seen: int,
        records_sent: int,
        records_skipped: int,
        outputs: tuple[PluginOutputRecord, ...],
        error_code: str | None,
    ) -> PluginExecution:
        execution = _summary(
            run_id,
            execution_id,
            manifest,
            status,
            records_seen,
            records_sent,
            records_skipped,
            outputs,
            error_code,
            started_at,
        )
        if session is not None:
            from .persistence import persist_plugin_execution

            persist_plugin_execution(session, execution)
        return execution

    if not manifest.enabled:
        return finish("disabled", len(documents), 0, len(documents), (), None)
    try:
        runner_path = _resolve_entrypoint_source(manifest.entrypoint)
        actual_hash = hashlib.sha256(runner_path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return finish("failed", len(documents), 0, len(documents), (), "plugin_unavailable")
    if actual_hash != manifest.runner_sha256:
        return finish("failed", len(documents), 0, len(documents), (), "runner_hash_mismatch")

    eligible = tuple(
        document for document in documents if document.quality_status in manifest.accepted_quality_statuses
    )
    inputs = tuple(_plugin_input(document, manifest.input_fields) for document in eligible)
    outputs: list[PluginOutputRecord] = []
    try:
        for offset in range(0, len(inputs), manifest.max_batch_records):
            batch = inputs[offset : offset + manifest.max_batch_records]
            job = PluginJob(
                protocol_version=PROTOCOL_VERSION,
                schema_fingerprint=SCHEMA_FINGERPRINT,
                plugin_id=manifest.plugin_id,
                plugin_version=manifest.plugin_version,
                entrypoint=manifest.entrypoint,
                output_schema_version=manifest.output_schema_version,
                output_fields=manifest.output_fields,
                output_types=manifest.output_types,
                records=batch,
                options=manifest.options,
            )
            result_document = await run_json_worker(
                module_name="omnisignal.processing.host",
                document=job.model_dump(mode="json"),
                workspace=workspace,
                timeout_seconds=manifest.timeout_seconds,
                max_output_bytes=manifest.max_output_bytes,
            )
            result = PluginResult.model_validate(result_document)
            _validate_result(result, manifest, batch)
            outputs.extend(result.outputs)
    except IsolatedWorkerError as exc:
        if exc.return_code in {20, 23}:
            error_code = "invalid_output"
        elif exc.return_code == 22:
            error_code = "plugin_crash"
        else:
            error_code = exc.kind.value
        return finish(
            "failed",
            len(documents),
            len(inputs),
            len(documents) - len(inputs),
            (),
            error_code,
        )
    except (OSError, ValueError, ValidationError):
        return finish(
            "failed",
            len(documents),
            len(inputs),
            len(documents) - len(inputs),
            (),
            "invalid_output",
        )
    ordered = tuple(sorted(outputs, key=lambda output: output.normalized_id))
    if len({output.normalized_id for output in ordered}) != len(ordered):
        return finish(
            "failed",
            len(documents),
            len(inputs),
            len(documents) - len(inputs),
            (),
            "duplicate_output_identity",
        )
    return finish(
        "succeeded",
        len(documents),
        len(inputs),
        len(documents) - len(inputs),
        ordered,
        None,
    )


def _resolve_entrypoint_source(entrypoint: str) -> Path:
    module_name, _, _ = entrypoint.partition(":")
    source_root = Path(__file__).resolve().parents[2]
    candidate = source_root.joinpath(*module_name.split(".")).with_suffix(".py").resolve()
    if source_root not in candidate.parents or not candidate.is_file():
        raise ValueError("plugin entrypoint must be a reviewed module inside project src")
    return candidate


def _plugin_input(document: NormalizedDocument, input_fields: tuple[str, ...]) -> PluginInputRecord:
    values = {field: _json_value(getattr(document, field)) for field in input_fields}
    return PluginInputRecord(
        normalized_id=document.normalized_id,
        source_id=document.source_id,
        quality_status=document.quality_status,
        fields=values,
    )


def _validate_result(
    result: PluginResult,
    manifest: PluginManifest,
    inputs: tuple[PluginInputRecord, ...],
) -> None:
    if (
        result.protocol_version != PROTOCOL_VERSION
        or result.schema_fingerprint != SCHEMA_FINGERPRINT
        or result.plugin_id != manifest.plugin_id
        or result.plugin_version != manifest.plugin_version
        or result.output_schema_version != manifest.output_schema_version
    ):
        raise ValueError("plugin result identity or schema differs from its manifest")
    input_ids = {record.normalized_id for record in inputs}
    output_ids = [output.normalized_id for output in result.outputs]
    if len(output_ids) != len(set(output_ids)) or not set(output_ids).issubset(input_ids):
        raise ValueError("plugin result contains invalid identities")
    if any(not set(output.values).issubset(manifest.output_fields) for output in result.outputs):
        raise ValueError("plugin result contains undeclared fields")
    if any(
        not output_value_matches_type(value, manifest.output_types[field])
        for output in result.outputs
        for field, value in output.values.items()
    ):
        raise ValueError("plugin result value type differs from its manifest")


def _execution_id(manifest: PluginManifest, documents: tuple[NormalizedDocument, ...]) -> str:
    inputs = [(document.normalized_id, document.normalized_hash) for document in documents]
    serialized = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{manifest.manifest_hash()}\x1f{serialized}".encode("utf-8")).hexdigest()


def _summary(
    run_id: str,
    execution_id: str,
    manifest: PluginManifest,
    status: str,
    records_seen: int,
    records_sent: int,
    records_skipped: int,
    outputs: tuple[PluginOutputRecord, ...],
    error_code: str | None,
    started_at: datetime,
) -> PluginExecution:
    serialized = json.dumps(
        [output.model_dump(mode="json") for output in outputs],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PluginExecution(
        run_id=run_id,
        execution_id=execution_id,
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.plugin_version,
        manifest_hash=manifest.manifest_hash(),
        output_schema_version=manifest.output_schema_version,
        status=status,
        records_seen=records_seen,
        records_sent=records_sent,
        records_skipped=records_skipped,
        output_count=len(outputs),
        output_hash=hashlib.sha256(serialized).hexdigest() if outputs else None,
        error_code=error_code,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        outputs=outputs,
    )


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value
