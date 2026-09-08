from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omnisignal.normalization import NormalizationConfig, normalize_record, run_normalization
from omnisignal.normalization import backfill_archive_lineage
from omnisignal.normalization.duplicates import cluster_duplicates
from omnisignal.connectors import FileRawResponseArchive
from omnisignal.storage import (
    Base,
    ContextEdge,
    DuplicateGroup,
    IngestedRecord,
    NormalizationRun,
    NormalizationRunMembership,
    NormalizedRecord,
    QualityEvent,
    create_database_engine,
)


def _config(*, min_content_chars: int = 10) -> NormalizationConfig:
    return NormalizationConfig.model_validate(
        {
            "config_version": "1.0",
            "profiles": [
                {
                    "source_id": "fixture_source",
                    "fields": {
                        "canonical_url": "url",
                        "title": "title",
                        "text": "body",
                        "published_at": "created",
                        "language": "lang",
                        "parent_source_record_id": "parent_id",
                    },
                    "required_fields": ["text"],
                    "default_language": "und",
                    "min_text_chars": 1,
                    "max_title_chars": 12,
                    "max_text_chars": 200,
                }
            ],
            "entities": [{"entity_id": "omnisignal", "aliases": ["OmniSignal", "Omni Signal"]}],
            "duplicate_policy": {
                "simhash_distance": 3,
                "ngram_size": 3,
                "min_content_chars": min_content_chars,
            },
        }
    )


def _record(
    source_record_id: str,
    payload: dict[str, object],
    *,
    source_id: str = "fixture_source",
    raw_hash: str | None = None,
    archive_sha: str | None = None,
) -> IngestedRecord:
    discriminator = source_record_id.encode("utf-8").hex()[:1] or "0"
    return IngestedRecord(
        source_id=source_id,
        source_record_id=source_record_id,
        payload=payload,
        raw_hash=raw_hash or discriminator * 64,
        schema_version="1.0",
        permission="fixture/v1",
        raw_archive_sha256=archive_sha if archive_sha is not None else "f" * 64,
    )


def test_config_hash_and_alias_ownership_are_deterministic() -> None:
    config = _config()
    assert config.config_hash() == _config().config_hash()
    with pytest.raises(ValidationError, match="cannot belong to multiple entities"):
        NormalizationConfig.model_validate(
            {
                **config.model_dump(mode="json"),
                "entities": [
                    {"entity_id": "first_entity", "aliases": ["ＯｍｎｉＳｉｇｎａｌ"]},
                    {"entity_id": "second_entity", "aliases": ["omnisignal"]},
                ],
            }
        )


def test_normalize_record_is_deterministic_and_records_quality_and_lineage() -> None:
    config = _config()
    record = _record(
        "item-1",
        {
            "url": "HTTPS://Example.COM:443/a#fragment",
            "title": "  ＯｍｎｉＳｉｇｎａｌ    heading that is long  ",
            "body": " first\r\n\r\n\r\n second ",
            "created": "2026-09-03T10:00:00+08:00",
            "lang": "en_us",
        },
    )

    first = normalize_record(record, config.profiles[0], config)
    second = normalize_record(record, config.profiles[0], config)

    assert first == second
    assert first.canonical_url == "https://example.com/a"
    assert first.title == "OmniSignal h"
    assert first.text == "first\n\nsecond"
    assert first.published_at == datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)
    assert first.language == "en-US"
    assert first.entity_ids == ("omnisignal",)
    assert first.raw_archive_sha256 == "f" * 64
    assert first.quality_status == "warning"
    assert ("field_truncated", "title") in {(issue.code, issue.field) for issue in first.issues}


def test_missing_archive_is_quarantined() -> None:
    config = _config()
    record = _record("missing-lineage", {"title": "Title", "body": "usable body"})
    record.raw_archive_sha256 = None

    document = normalize_record(record, config.profiles[0], config)

    assert document.quality_status == "quarantined"
    assert "missing_raw_archive" in {issue.code for issue in document.issues}


def test_short_exact_duplicates_are_grouped_without_simhash() -> None:
    config = _config(min_content_chars=100)
    left = normalize_record(_record("left", {"title": "Same", "body": "same"}), config.profiles[0], config)
    right = normalize_record(_record("right", {"title": "Same", "body": "same"}), config.profiles[0], config)
    assert left.simhash64 is None and right.simhash64 is None

    clusters = cluster_duplicates((left, right), normalization_run_id="run", max_distance=3)

    assert len(clusters) == 1
    assert clusters[0].duplicate_kind == "exact"
    assert set(clusters[0].normalized_ids) == {left.normalized_id, right.normalized_id}


def test_repository_replays_idempotently_and_preserves_old_versions() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    config = _config()
    common = {"title": "Duplicate", "body": "same searchable content", "lang": "zh-CN"}
    records = [
        _record("parent", {"title": "Parent", "body": "parent context", "lang": "zh-CN"}),
        _record(
            "child",
            {"title": "Child", "body": "child context", "lang": "zh-CN", "parent_id": "parent"},
        ),
        _record("duplicate-a", {**common, "url": "https://example.com/a"}),
        _record("duplicate-b", {**common, "url": "https://example.com/b"}),
        _record("missing-parent", {"title": "Orphan", "body": "orphan context", "parent_id": "absent"}),
        _record("outside", {"body": "not configured"}, source_id="unconfigured_source"),
    ]

    with Session(engine) as session:
        session.add_all(records)
        session.commit()
        first = run_normalization(session, config)
        second = run_normalization(session, config)

        assert first.input_count == 6
        assert first.normalized_count == 5
        assert first.unconfigured_count == 1
        assert first.duplicate_group_count == 1
        assert first.context_edge_count == 1
        assert first.replay_reused is False
        assert second.run_id == first.run_id
        assert second.replay_reused is True
        assert second.reused_record_count == 5
        assert session.scalar(select(func.count()).select_from(NormalizationRun)) == 1
        assert session.scalar(select(func.count()).select_from(NormalizedRecord)) == 5
        assert session.scalar(select(func.count()).select_from(NormalizationRunMembership)) == 6
        memberships = tuple(
            session.scalars(
                select(NormalizationRunMembership).order_by(
                    NormalizationRunMembership.source_id,
                    NormalizationRunMembership.source_record_id,
                )
            )
        )
        assert {membership.disposition for membership in memberships} == {
            "normalized_new",
            "unconfigured",
        }
        assert sum(membership.normalized_id is None for membership in memberships) == 1
        assert session.scalar(select(func.count()).select_from(ContextEdge)) == 1
        assert session.scalar(select(func.count()).select_from(DuplicateGroup)) == 1
        assert session.scalar(
            select(func.count()).select_from(QualityEvent).where(QualityEvent.code == "context_target_missing")
        ) == 1

        changed = session.get(IngestedRecord, ("fixture_source", "child"))
        assert changed is not None
        changed.payload = {**changed.payload, "body": "child context changed"}
        changed.raw_hash = "e" * 64
        session.commit()
        third = run_normalization(session, config)

        assert third.run_id != first.run_id
        assert third.reused_record_count == 4
        assert session.scalar(select(func.count()).select_from(NormalizedRecord)) == 6
        assert (
            session.scalar(
                select(func.count())
                .select_from(NormalizationRunMembership)
                .where(NormalizationRunMembership.normalization_run_id == third.run_id)
            )
            == 6
        )
    engine.dispose()


def test_archive_lineage_backfill_requires_exact_identity_and_raw_hash(tmp_path) -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    payload = {"title": "Archived", "body": "archived body"}
    raw_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    archive = FileRawResponseArchive(tmp_path / "raw")
    receipt = archive.store(
        source_id="fixture_source",
        request_url="https://example.com/item",
        status_code=200,
        headers={},
        body={"record_links": [{"source_record_id": "archived", "raw_hash": raw_hash}]},
        collected_at=datetime.now(timezone.utc),
    )

    with Session(engine) as session:
        record = _record("archived", payload, raw_hash=raw_hash)
        record.raw_archive_sha256 = None
        mismatch = _record("mismatch", payload, raw_hash="0" * 64)
        mismatch.raw_archive_sha256 = None
        session.add_all([record, mismatch])
        session.commit()

        summary = backfill_archive_lineage(session, tmp_path / "raw")

        assert summary.archives_scanned == 1
        assert summary.missing_records_seen == 2
        assert summary.records_matched == 1
        assert record.raw_archive_sha256 == receipt.sha256
        assert mismatch.raw_archive_sha256 is None
    engine.dispose()
