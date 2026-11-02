"""Operator-scoped internal delivery grants, separate from image/content revisions.

These are operational instructions, not model-inferred ownership or copyright
certificates. Original permissions remain untouched. Only the signed delivery
projection receives the narrowly scoped internal-use basis.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .store import Conflict, require

PURPOSE = "company_internal_inspiration"
SOURCES = {"potw", "adsofbrands", "bpando", "itsnicethat"}
POLICY_KEYS = {"policy_id", "authorized_at", "purpose", "source_ids", "recipient"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def validate_policy(value, *, source_ids=None):
    if value is None:
        return None
    require(isinstance(value, dict) and set(value) == POLICY_KEYS, "invalid_publication_authorization")
    require(isinstance(value["policy_id"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value["policy_id"])
            and value["purpose"] == PURPOSE, "invalid_publication_authorization")
    sources = value["source_ids"]
    require(isinstance(sources, list) and bool(sources) and all(isinstance(s, str) and s in SOURCES for s in sources)
            and len(sources) == len(set(sources)), "invalid_publication_source_scope")
    require(source_ids is None or set(sources) <= set(source_ids), "invalid_publication_source_scope")
    try:
        require(isinstance(value["authorized_at"], str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value["authorized_at"]),
                "invalid_publication_authorization")
        require(datetime.fromisoformat(value["authorized_at"]).tzinfo is not None, "invalid_publication_authorization")
        recipient = value["recipient"]
        require(isinstance(recipient, dict) and set(recipient) == {"origin", "workspace_id"}, "invalid_publication_recipient")
        origin = recipient["origin"]
        require(isinstance(origin, str) and len(origin) <= 2048, "invalid_publication_recipient")
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        port = parsed.port
        # Use an intentionally narrow canonical DNS/IPv4 origin subset. This
        # avoids signing an origin that the receiver's WHATWG URL normalizes to
        # a different string (case, default ports, abbreviated numeric hosts).
        require(host and len(host) <= 253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                                for label in host.split(".")), "invalid_publication_recipient")
        if host.split(".")[-1].isdigit():
            require(str(ipaddress.IPv4Address(host)) == host, "invalid_publication_recipient")
        canonical_origin = "https://" + host + (f":{port}" if port is not None else "")
        require(parsed.scheme == "https" and port != 443 and parsed.path == "" and not parsed.query
                and not parsed.fragment and not parsed.username and not parsed.password
                and origin == canonical_origin, "invalid_publication_recipient")
        require(isinstance(recipient["workspace_id"], str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", recipient["workspace_id"]), "invalid_publication_recipient")
    except (TypeError, ValueError):
        raise Conflict("invalid_publication_authorization") from None
    return deepcopy(value)


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS publication_use_grants(
        asset_id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
        PRIMARY KEY(asset_id,revision))""")


def permits_internal_delivery(asset, policy):
    permissions = asset.get("permissions", {})
    if all(permissions.get(name) == "allowed" for name in ("display", "export")):
        return True
    return bool(policy and asset.get("source_id") in policy["source_ids"]
                and asset.get("status") == "accepted" and asset.get("publication_status") != "withdrawn"
                and permissions.get("analysis") == "allowed"
                and all(permissions.get(name) in {"allowed", "unknown"} for name in ("display", "export"))
                and asset.get("reviewed_analysis", {}).get("provenance") == "model_automated_review")


def _basis(asset, policy):
    # Bind the accepted input and its evidence. Publication markers are delivery
    # outputs; changing them must not invalidate an otherwise identical grant.
    bound = {key: value for key, value in asset.items() if key not in
             {"publication_status", "publication_version", "image_path", "publication_use_basis", "audit"}}
    return {"schema_version": 1, "policy_id": policy["policy_id"], "policy_sha256": digest(policy),
            "purpose": PURPOSE, "authorized_at": policy["authorized_at"], "source_id": asset["source_id"],
            "asset_id": asset["id"], "revision": asset["revision"], "sha256": asset["sha256"],
            "input_binding": digest(bound), "recipient": deepcopy(policy["recipient"]),
            "authorization_source": "operator_policy"}


def existing_basis(db, asset):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_use_grants'").fetchone():
        return None
    row = db.execute("SELECT document FROM publication_use_grants WHERE asset_id=? AND revision=?",
                     (asset["id"], asset["revision"])).fetchone()
    if row is None:
        return None
    grant = json.loads(row[0])
    require(isinstance(grant, dict) and set(grant) == {"policy", "basis"}, "publication_use_grant_invalid")
    policy = validate_policy(grant["policy"])
    require(permits_internal_delivery(asset, policy) and asset.get("publication_status") != "withdrawn"
            and grant["basis"] == _basis(asset, policy), "publication_use_grant_stale")
    return grant["basis"]


def delivery_projection(store, db, asset, *, policy=None, create=False):
    """Called inside the signing transaction; never changes the source document."""
    projected = deepcopy(asset)
    if all(asset.get("permissions", {}).get(name) == "allowed" for name in ("display", "export")):
        return projected
    policy = validate_policy(policy)
    basis = existing_basis(db, asset)
    if create:
        require(permits_internal_delivery(asset, policy), "display_export_permission_required")
        require(policy is not None, "publication_policy_required")
        wanted = _basis(asset, policy)
        require(basis is None or basis == wanted, "publication_policy_changed")
        if basis is None:
            ensure_table(db)
            basis = wanted
            db.execute("INSERT INTO publication_use_grants VALUES(?,?,?)",
                       (asset["id"], asset["revision"], json.dumps({"policy": policy, "basis": basis}, ensure_ascii=False)))
            store.audit(db, asset["id"], "system:auto-editorial", "internal_publication_authorized", basis)
    require(basis is not None, "display_export_permission_required")
    projected["permissions"] = {**asset["permissions"], "display": "allowed", "export": "allowed"}
    projected["publication_use_basis"] = deepcopy(basis)
    return projected
