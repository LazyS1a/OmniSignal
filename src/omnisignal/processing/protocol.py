"""Versioned JSON boundary between the core and the Pluggy host."""

from __future__ import annotations

import hashlib
import json


PROTOCOL_VERSION = "1.0"
WIRE_SCHEMA = {
    "job": "plugin identity, selected normalized fields, options",
    "result": "plugin identity, versioned output records",
    "transport": "bounded JSON files",
}
SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(WIRE_SCHEMA, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
