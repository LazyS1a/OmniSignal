"""Recover exact record-to-archive lineage from verified content-addressed archives."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnisignal.storage.models import IngestedRecord


_MAX_ARCHIVE_BYTES = 50_000_000


@dataclass(frozen=True, slots=True)
class LineageBackfillSummary:
    archives_scanned: int
    missing_records_seen: int
    records_matched: int
    records_updated: int
    ambiguous_matches: int


def backfill_archive_lineage(session: Session, archive_root: Path) -> LineageBackfillSummary:
    """Fill only null lineage fields when an archive proves the exact stored raw hash."""
    root = archive_root.resolve()
    candidates: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    archives_scanned = 0
    if root.is_dir():
        for path in sorted(root.rglob("*.json.gz")):
            resolved = path.resolve()
            if root not in resolved.parents:
                raise RuntimeError("archive scan escaped the configured root")
            document, digest = _verified_archive(resolved)
            archives_scanned += 1
            source_id = document.get("source_id")
            body = document.get("body")
            if not isinstance(source_id, str) or not isinstance(body, dict):
                raise RuntimeError(f"archive envelope is invalid: {resolved.relative_to(root)}")
            for source_record_id, raw_hash in _record_links(body):
                candidates[(source_id, source_record_id, raw_hash)].add(digest)

    records = tuple(
        session.scalars(
            select(IngestedRecord)
            .where(IngestedRecord.raw_archive_sha256.is_(None))
            .order_by(IngestedRecord.source_id, IngestedRecord.source_record_id)
        )
    )
    matched = 0
    updated = 0
    ambiguous = 0
    for record in records:
        digests = candidates.get((record.source_id, record.source_record_id, record.raw_hash), set())
        if not digests:
            continue
        matched += 1
        ambiguous += len(digests) > 1
        record.raw_archive_sha256 = min(digests)
        updated += 1
    session.flush()
    return LineageBackfillSummary(
        archives_scanned=archives_scanned,
        missing_records_seen=len(records),
        records_matched=matched,
        records_updated=updated,
        ambiguous_matches=ambiguous,
    )


def _verified_archive(path: Path) -> tuple[dict[str, Any], str]:
    try:
        with gzip.open(path, "rb") as stream:
            serialized = stream.read(_MAX_ARCHIVE_BYTES + 1)
        if len(serialized) > _MAX_ARCHIVE_BYTES:
            raise RuntimeError("archive exceeded the 50 MB verification limit")
        digest = hashlib.sha256(serialized).hexdigest()
        expected = path.name.removesuffix(".json.gz")
        if digest != expected:
            raise RuntimeError("archive content digest does not match its filename")
        document = json.loads(serialized)
        if not isinstance(document, dict):
            raise RuntimeError("archive envelope must be an object")
        return document, digest
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"archive is unreadable: {path.name}") from exc


def _record_links(body: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    explicit = body.get("record_links")
    if explicit is not None:
        if not isinstance(explicit, list):
            raise RuntimeError("archive record_links must be a list")
        links: list[tuple[str, str]] = []
        for value in explicit:
            if not isinstance(value, dict):
                raise RuntimeError("archive record link must be an object")
            source_record_id = value.get("source_record_id")
            raw_hash = value.get("raw_hash")
            if not _valid_link(source_record_id, raw_hash):
                raise RuntimeError("archive record link is invalid")
            links.append((source_record_id, raw_hash))
        return tuple(links)

    # Legacy GitHub archives stored projected records under items. Legacy Hook
    # archives stored the worker records under records. Both retain enough bytes
    # to reproduce the connector's exact raw hash without guessing.
    key = "items" if isinstance(body.get("items"), list) else "records"
    values = body.get(key)
    if not isinstance(values, list):
        return ()
    links = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("source_record_id"), str):
            raise RuntimeError(f"legacy archive {key} entry is invalid")
        source_record_id = value["source_record_id"]
        hashed_value = value if key == "records" else {k: v for k, v in value.items() if k != "source_record_id"}
        raw_hash = hashlib.sha256(
            json.dumps(hashed_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        links.append((source_record_id, raw_hash))
    return tuple(links)


def _valid_link(source_record_id: object, raw_hash: object) -> bool:
    return (
        isinstance(source_record_id, str)
        and 0 < len(source_record_id) <= 512
        and isinstance(raw_hash, str)
        and len(raw_hash) == 64
        and all(character in "0123456789abcdef" for character in raw_hash)
    )
