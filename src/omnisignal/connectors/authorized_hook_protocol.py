"""Versioned JSON boundary shared by authorized Hook workers and the core connector."""

from __future__ import annotations

import hashlib
import json


PROTOCOL_VERSION = "1.0"
OUTPUT_SCHEMA = {
    "protocol_version": "1.x",
    "status": "ok|error",
    "target": "registered target",
    "schema_fingerprint": "sha256",
    "records": "array<object>",
    "next_cursor": "string|null",
    "has_more": "boolean",
    "error": "object|null",
}
OUTPUT_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(OUTPUT_SCHEMA, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
