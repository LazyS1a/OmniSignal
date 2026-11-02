"""Explicit, signed library recipients. No workspace-to-library inference."""
from copy import deepcopy
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_library_recipient(value):
    if not isinstance(value, dict) or set(value) != {"kind", "origin", "library_id"} or value["kind"] != "library":
        raise ValueError("invalid_library_recipient")
    if not isinstance(value["library_id"], str) or not IDENTIFIER.fullmatch(value["library_id"]):
        raise ValueError("invalid_library_recipient")
    origin = value["origin"]
    if not isinstance(origin, str) or len(origin) > 2048:
        raise ValueError("invalid_library_recipient")
    parsed = urlsplit(origin)
    host, port = parsed.hostname or "", parsed.port
    if not host or len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
        raise ValueError("invalid_library_recipient")
    if host.split(".")[-1].isdigit() and str(ipaddress.IPv4Address(host)) != host:
        raise ValueError("invalid_library_recipient")
    expected = "https://" + host + (f":{port}" if port is not None else "")
    if parsed.scheme != "https" or port == 443 or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or origin != expected:
        raise ValueError("invalid_library_recipient")
    return deepcopy(value)


def recipient_key(recipient):
    value = validate_library_recipient(recipient)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def envelope_recipient(value):
    version = value.get("schema_version")
    if type(version) is int and version == 1 and "recipient" not in value:
        return None
    if type(version) is int and version == 2:
        return validate_library_recipient(value.get("recipient"))
    raise ValueError("invalid_recipient_schema")
