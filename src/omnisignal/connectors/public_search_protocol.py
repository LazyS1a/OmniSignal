"""Versioned JSON boundary for the public search-signals worker."""

from __future__ import annotations

import hashlib
import json


PROTOCOL_VERSION = "1.0"
COLLECTOR_VERSION = "pytrends/4.9.2+google-suggest/1"
OUTPUT_SCHEMA = {
    "protocol_version": "1.x",
    "schema_fingerprint": "sha256",
    "status": "ok|error",
    "records": "array<public-search-signal>",
    "warnings": "array<string>",
    "fetched_at": "rfc3339",
    "error": "object|null",
}
OUTPUT_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(OUTPUT_SCHEMA, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
