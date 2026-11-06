"""Read-only downstream receipt gate for automatic publication.

Signed-but-unreceived content consumes the producer's available allowance until
the receiver confirms its hash. Receipt lookup never grants material use rights.
The receiver still enforces the final quota in its install transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from .publishing import current_publication_cases
from .library_publication import target_root

CONFIG_ENV = "OMNISIGNAL_WORKBENCH_RECEIPT_CONFIG"
HASH = re.compile(r"[a-f0-9]{64}")


class ReceiptUnavailable(ValueError):
    pass


def _require(condition):
    if not condition:
        raise ReceiptUnavailable("receiver_receipt_invalid")


class ReceiptClient:
    def __init__(self, origin, token, *, client=None):
        p = urlsplit(origin)
        _require(p.scheme == "https" and p.hostname and not p.username and not p.password
                 and p.port in (None, 443) and p.path in ("", "/") and not p.query and not p.fragment)
        _require(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,512}", token.strip()))
        self.origin, self.token, self.client = origin.rstrip("/"), token.strip(), client

    def preflight(self, hashes):
        _require(len(hashes) <= 1000 and all(HASH.fullmatch(x) for x in hashes))
        owned = self.client is None
        client = self.client or httpx.Client(timeout=20, follow_redirects=False)
        try:
            with client.stream("POST", self.origin + "/api/internal/inspiration-receipts/preflight",
                               headers={"Authorization": "Bearer " + self.token},
                               json={"sha256": hashes}, timeout=20, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ReceiptUnavailable("receiver_receipt_unavailable")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 256 * 1024:
                        raise ReceiptUnavailable("receiver_receipt_invalid")
                return json.loads(body)
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeError):
            raise ReceiptUnavailable("receiver_receipt_unavailable") from None
        finally:
            if owned:
                client.close()


def configured_client(policy):
    path = os.getenv(CONFIG_ENV)
    if not path:
        return None
    try:
        file = Path(path)
        _require(file.is_file() and not file.is_symlink() and file.stat().st_size <= 16000)
        config = json.loads(file.read_text(encoding="utf-8"))
        _require(isinstance(config, dict) and set(config) == {"schema_version", "origin", "token_file", "recipient"}
                 and config["schema_version"] == 1 and policy is not None
                 and config["recipient"] == policy.get("recipient"))
        # Even a valid receipt token must refer to this exact delivery audience.
        _require(config["origin"].rstrip("/") == policy["recipient"].get("origin", "").rstrip("/"))
        token_file = Path(config["token_file"])
        _require(token_file.is_absolute() and token_file.is_file() and not token_file.is_symlink()
                 and token_file.stat().st_size <= 4096)
        return ReceiptClient(config["origin"], token_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ReceiptUnavailable("receiver_receipt_config_invalid") from None


def _validate(value, requested, day):
    _require(isinstance(value, dict) and value.get("schema_version") == 1
             and value.get("timezone") == "Asia/Shanghai" and value.get("day") == day
             and value.get("metric") == "first_successful_unique_image_sha256"
             and value.get("advisory") is True and value.get("atomic_package_required") is True)
    if value.get("enabled") is not True or value.get("initialized") is not True:
        raise ReceiptUnavailable("receiver_daily_policy_not_initialized")
    for key in ("received", "target", "remaining", "baseline_count", "new_unique_images"):
        _require(type(value.get(key)) is int and value[key] >= 0)
    _require(value["target"] in (100, 200, 300, 400, 500)
             and value["remaining"] == max(0, value["target"] - value["received"]))
    for key in ("baseline_revision", "receipt_revision"):
        _require(isinstance(value.get(key), str) and 0 < len(value[key]) <= 256)
    known = value.get("known_sha256")
    _require(isinstance(known, list) and all(isinstance(x, str) and HASH.fullmatch(x) for x in known)
             and len(known) == len(set(known)) and set(known) <= set(requested)
             and value["new_unique_images"] == len(set(requested) - set(known)))
    return set(known)


def select_publication_batch(store, batch, policy, *, publication_root=None, client=None):
    """Return a bounded subset; a caller MUST keep an already frozen batch whole.

    Caller holds the auto-editorial process lock. Network calls occur outside a
    database write transaction. Only observed, permanent receiver hashes cache.
    """
    client = client or configured_client(policy)
    if client is None:
        return batch, {"status": "disabled", "receipt_status": "unconfirmed"}
    day = datetime.fromtimestamp(store.clock(), timezone(timedelta(hours=8))).date().isoformat()
    head = client.preflight([])
    _validate(head, [], day)
    key = "receiver_known.v1:" + hashlib.sha256(json.dumps(
        {"origin": client.origin, "recipient": policy["recipient"]}, sort_keys=True).encode()).hexdigest()
    with store.db() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    cache = json.loads(row[0]) if row else {}
    _require(isinstance(cache, dict) and isinstance(cache.get("sha256", []), list)
             and len(cache.get("sha256", [])) <= 1000000)
    known = set(cache.get("sha256", [])) if cache.get("baseline_revision") == head["baseline_revision"] else set()
    _require(all(isinstance(x, str) and HASH.fullmatch(x) for x in known))
    root = target_root(store, policy, publication_root)
    records = current_publication_cases(root)
    # Publisher images are content-addressed; legacy/malformed paths fail closed.
    paths = [image["path"] for record in records.values() for image in record.get("images", [])]
    _require(all(isinstance(path, str) and re.fullmatch(r"images/[a-f0-9]{64}\.(?:jpg|jpeg|png|webp)", path) for path in paths))
    signed = {Path(path).stem for path in paths}
    wanted = signed | {item["sha256"] for item in batch}
    _require(all(isinstance(x, str) and HASH.fullmatch(x) for x in wanted))
    unknown = sorted(wanted - known)
    # Bound work per tick; pending backlog is retained and operators see the gap.
    _require(len(unknown) <= 10000)
    for start in range(0, len(unknown), 1000):
        chunk = unknown[start:start + 1000]
        reply = client.preflight(chunk)
        observed = _validate(reply, chunk, day)
        if any(reply[key] != head[key] for key in ("receipt_revision", "baseline_revision", "remaining", "target")):
            raise ReceiptUnavailable("receiver_receipt_changed")
        known.update(observed)
    with store.db() as db:
        db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps({"baseline_revision": head["baseline_revision"], "sha256": sorted(known)})))
    outstanding = signed - known
    available = max(0, head["remaining"] - len(outstanding))
    selected, reserved = [], set()
    for item in batch:
        sha = item["sha256"]
        if sha in known or sha in outstanding or sha in reserved:
            selected.append(item)
        elif len(reserved) < available:
            selected.append(item)
            reserved.add(sha)
    return selected, {"status": "ready", "receipt_status": "receiver_confirmed",
        "day": day, "target": head["target"], "received": head["received"],
        "remaining": head["remaining"], "signed_unreceived": len(outstanding),
        "available_to_publish": available, "selected_new_unique": len(reserved),
        "human_reviewed": None}
