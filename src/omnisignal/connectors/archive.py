"""Content-addressed, policy-filtered response archive."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


_SAFE_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "etag",
        "last-modified",
        "link",
        "x-github-api-version-selected",
    }
)


@dataclass(frozen=True, slots=True)
class ArchiveReceipt:
    path: Path
    sha256: str
    compressed_bytes: int
    reused: bool


class FileRawResponseArchive:
    """Store sanitized response envelopes as deterministic gzip files."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        *,
        source_id: str,
        request_url: str,
        status_code: int,
        headers: Mapping[str, str],
        body: dict[str, Any],
        collected_at: datetime,
    ) -> ArchiveReceipt:
        if _SAFE_SOURCE_ID.fullmatch(source_id) is None:
            raise ValueError("unsafe source_id for response archive")

        safe_headers = {
            key.lower(): value
            for key, value in headers.items()
            if key.lower() in _SAFE_RESPONSE_HEADERS
        }
        envelope = {
            "archive_schema_version": "1.0",
            "source_id": source_id,
            "request_url": request_url,
            "status_code": status_code,
            "headers": safe_headers,
            "body": body,
        }
        serialized = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(serialized).hexdigest()
        # Observation times live in ingestion_runs/records. The blob key deliberately
        # excludes time and volatile rate-limit headers so identical responses reuse bytes.
        target_dir = self.root / source_id / digest[:2]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = (target_dir / f"{digest}.json.gz").resolve()
        if self.root not in target.parents:
            raise ValueError("archive path escaped configured root")
        if target.exists():
            return ArchiveReceipt(target, digest, target.stat().st_size, True)

        compressed = gzip.compress(serialized, compresslevel=6, mtime=0)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(compressed)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return ArchiveReceipt(target, digest, len(compressed), False)
