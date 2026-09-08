"""Versioned JSON boundary for the isolated SearXNG worker."""

from __future__ import annotations

import hashlib
import json


PROTOCOL_VERSION = "1.1"
COLLECTOR_VERSION = "searxng-http-json/1"
OUTPUT_SCHEMA = {
    "protocol_version": "1.x",
    "schema_fingerprint": "sha256",
    "status": "ok|error",
    "records": "array<searxng-result-snapshot>",
    "warnings": "array<string>",
    "fetched_at": "rfc3339",
    "error": "object|null",
    "diagnostics?": "array<query,engine,record_count,status,reason>",
}
OUTPUT_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(OUTPUT_SCHEMA, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
