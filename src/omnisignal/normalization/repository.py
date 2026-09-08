"""Transactional replay of active ingested records into immutable normalized versions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnisignal.storage.models import (
    ContextEdge,
    DuplicateGroup,
    DuplicateMembership,
    IngestedRecord,
    NormalizationRun,
    NormalizationRunMembership,
    NormalizedRecord,
    QualityEvent,
)

from .contracts import NormalizationConfig
from .duplicates import cluster_duplicates
from .processor import NORMALIZER_VERSION, NormalizedDocument, QualityIssue, normalize_record


@dataclass(frozen=True, slots=True)
class NormalizationSummary:
    run_id: str
    input_count: int
    normalized_count: int
    reused_record_count: int
    unconfigured_count: int
    warning_count: int
    quarantined_count: int
    duplicate_group_count: int
    context_edge_count: int
    replay_reused: bool


def run_normalization(session: Session, config: NormalizationConfig) -> NormalizationSummary:
    records = tuple(
        session.scalars(
            select(IngestedRecord)
            .where(IngestedRecord.deleted_at.is_(None))
            .order_by(IngestedRecord.source_id, IngestedRecord.source_record_id)
        )
    )
    config_hash = config.config_hash()
    input_set_hash = _input_set_hash(records)
    run_id = _digest("normalization-run", NORMALIZER_VERSION, config_hash, input_set_hash)
    existing_run = session.get(NormalizationRun, run_id)
    if existing_run is not None:
        if existing_run.status != "succeeded":
            raise RuntimeError("existing deterministic normalization run is not complete")
        _ensure_memberships(session, existing_run, records, config)
        return _summary_from_run(session, existing_run, replay_reused=True)

    run = NormalizationRun(
        run_id=run_id,
        config_version=config.config_version,
        config_hash=config_hash,
        normalizer_version=NORMALIZER_VERSION,
        input_set_hash=input_set_hash,
        status="running",
        input_count=len(records),
        normalized_count=0,
        unconfigured_count=0,
        warning_count=0,
        quarantined_count=0,
        duplicate_group_count=0,
    )
    session.add(run)
    session.flush()

    documents: list[NormalizedDocument] = []
    unconfigured = 0
    for record in records:
        profile = config.profile_for(record.source_id)
        if profile is None:
            unconfigured += 1
            session.add(_membership(run_id, record, None, "unconfigured"))
            continue
        documents.append(normalize_record(record, profile, config))
    ordered_documents = tuple(sorted(documents, key=lambda document: document.normalized_id))
    clusters = cluster_duplicates(
        ordered_documents,
        normalization_run_id=run_id,
        max_distance=config.duplicate_policy.simhash_distance,
    )

    reused_records = 0
    for document in ordered_documents:
        stored = session.get(NormalizedRecord, document.normalized_id)
        if stored is not None:
            if stored.normalized_hash != document.normalized_hash:
                raise RuntimeError("deterministic normalization produced a conflicting hash")
            reused_records += 1
            source_record = next(
                record
                for record in records
                if record.source_id == document.source_id
                and record.source_record_id == document.source_record_id
            )
            session.add(_membership(run_id, source_record, document.normalized_id, "normalized_reused"))
            continue
        session.add(_to_model(document))
        source_record = next(
            record
            for record in records
            if record.source_id == document.source_id
            and record.source_record_id == document.source_record_id
        )
        session.add(_membership(run_id, source_record, document.normalized_id, "normalized_new"))
    session.flush()

    for document in ordered_documents:
        for issue in document.issues:
            session.add(_quality_event(run_id, document.normalized_id, issue))

    current_by_source_identity = {
        (document.source_id, document.source_record_id): document for document in ordered_documents
    }
    context_edges = 0
    for document in ordered_documents:
        if document.parent_source_record_id is None:
            continue
        parent = current_by_source_identity.get((document.source_id, document.parent_source_record_id))
        if parent is None:
            session.add(
                _quality_event(
                    run_id,
                    document.normalized_id,
                    QualityIssue("context_target_missing", "warning", "parent_source_record_id"),
                )
            )
            continue
        edge_id = _digest(run_id, document.normalized_id, parent.normalized_id, "parent")
        session.add(
            ContextEdge(
                edge_id=edge_id,
                normalization_run_id=run_id,
                from_normalized_id=document.normalized_id,
                to_normalized_id=parent.normalized_id,
                relation="parent",
            )
        )
        context_edges += 1

    for cluster in clusters:
        session.add(
            DuplicateGroup(
                group_id=cluster.group_id,
                normalization_run_id=run_id,
                duplicate_kind=cluster.duplicate_kind,
                member_count=len(cluster.normalized_ids),
            )
        )
        for normalized_id in cluster.normalized_ids:
            session.add(DuplicateMembership(group_id=cluster.group_id, normalized_id=normalized_id))

    run.normalized_count = len(ordered_documents)
    run.unconfigured_count = unconfigured
    run.warning_count = sum(document.quality_status == "warning" for document in ordered_documents)
    run.quarantined_count = sum(document.quality_status == "quarantined" for document in ordered_documents)
    run.duplicate_group_count = len(clusters)
    run.status = "succeeded"
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return NormalizationSummary(
        run_id=run_id,
        input_count=len(records),
        normalized_count=len(ordered_documents),
        reused_record_count=reused_records,
        unconfigured_count=unconfigured,
        warning_count=run.warning_count,
        quarantined_count=run.quarantined_count,
        duplicate_group_count=len(clusters),
        context_edge_count=context_edges,
        replay_reused=False,
    )


def _to_model(document: NormalizedDocument) -> NormalizedRecord:
    return NormalizedRecord(
        normalized_id=document.normalized_id,
        source_id=document.source_id,
        source_record_id=document.source_record_id,
        input_raw_hash=document.input_raw_hash,
        raw_archive_sha256=document.raw_archive_sha256,
        permission=document.permission,
        source_schema_version=document.source_schema_version,
        normalizer_version=document.normalizer_version,
        config_hash=document.config_hash,
        normalized_hash=document.normalized_hash,
        canonical_url=document.canonical_url,
        title=document.title,
        text=document.text,
        published_at=document.published_at,
        updated_at=document.updated_at,
        language=document.language,
        entity_ids=list(document.entity_ids),
        exact_fingerprint=document.exact_fingerprint,
        simhash64=document.simhash64,
        parent_source_record_id=document.parent_source_record_id,
        quality_status=document.quality_status,
        quality_codes=sorted({issue.code for issue in document.issues}),
    )


def _membership(
    run_id: str,
    record: IngestedRecord,
    normalized_id: str | None,
    disposition: str,
) -> NormalizationRunMembership:
    return NormalizationRunMembership(
        normalization_run_id=run_id,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        input_raw_hash=record.raw_hash,
        normalized_id=normalized_id,
        disposition=disposition,
    )


def _ensure_memberships(
    session: Session,
    run: NormalizationRun,
    records: tuple[IngestedRecord, ...],
    config: NormalizationConfig,
) -> None:
    existing_count = len(
        tuple(
            session.scalars(
                select(NormalizationRunMembership.source_record_id).where(
                    NormalizationRunMembership.normalization_run_id == run.run_id
                )
            )
        )
    )
    if existing_count == run.input_count:
        return
    if existing_count != 0:
        raise RuntimeError("normalization run membership is incomplete")
    for record in records:
        profile = config.profile_for(record.source_id)
        if profile is None:
            session.add(_membership(run.run_id, record, None, "unconfigured"))
            continue
        document = normalize_record(record, profile, config)
        stored = session.get(NormalizedRecord, document.normalized_id)
        if stored is None or stored.normalized_hash != document.normalized_hash:
            raise RuntimeError("normalization run membership cannot be reconstructed")
        session.add(_membership(run.run_id, record, document.normalized_id, "normalized_reused"))
    session.commit()


def _quality_event(run_id: str, normalized_id: str, issue: QualityIssue) -> QualityEvent:
    event_id = _digest(run_id, normalized_id, issue.code, issue.field or "")
    return QualityEvent(
        event_id=event_id,
        normalization_run_id=run_id,
        normalized_id=normalized_id,
        code=issue.code,
        severity=issue.severity,
        field=issue.field,
        detail={},
    )


def _input_set_hash(records: tuple[IngestedRecord, ...]) -> str:
    values = [
        {
            "source_id": record.source_id,
            "source_record_id": record.source_record_id,
            "raw_hash": record.raw_hash,
            "schema_version": record.schema_version,
            "permission": record.permission,
            "raw_archive_sha256": record.raw_archive_sha256,
        }
        for record in records
    ]
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _summary_from_run(session: Session, run: NormalizationRun, *, replay_reused: bool) -> NormalizationSummary:
    context_edges = len(
        tuple(session.scalars(select(ContextEdge.edge_id).where(ContextEdge.normalization_run_id == run.run_id)))
    )
    return NormalizationSummary(
        run_id=run.run_id,
        input_count=run.input_count,
        normalized_count=run.normalized_count,
        reused_record_count=run.normalized_count,
        unconfigured_count=run.unconfigured_count,
        warning_count=run.warning_count,
        quarantined_count=run.quarantined_count,
        duplicate_group_count=run.duplicate_group_count,
        context_edge_count=context_edges,
        replay_reused=replay_reused,
    )


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
