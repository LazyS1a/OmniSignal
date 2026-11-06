"""Bounded, recipient-bound snapshots with reusable content objects.

Offline publisher only. v1/v2 files and pointers are never rewritten. A signed
snapshot proves publication, not consumer receipt, model quality or new rights.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from PIL import Image

from .release import (KnowledgeError, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, MAX_RECORD_BYTES,
    _atomic_json, _decode, _file, _identifier, _image, _integer, _load, _lock,
    _record, _record_scope, _release_manifests, _require, _signed, _text, _withdrawal_files, canonical,
    digest, read_latest, utc_stamp, verify_release, verify_signature)
from .scope import validate_library_recipient, envelope_recipient

ENABLE_ENV = "OMNISIGNAL_SNAPSHOT_V3_ENABLED"
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_PART_RECORDS = 2000
TARGET_PART_RECORDS = 500
MAX_PARTS = 4096
MAX_SNAPSHOT_RECORDS = 100_000
MAX_SNAPSHOT_IMAGES = 1_000_000
MAX_SNAPSHOT_IMAGE_BYTES = 100 * 1024**3
DEFAULT_IMAGE_BUDGET = 2 * 1024**3
DEFAULT_RETAINED_BUDGET = 4 * 1024**3
HASH = re.compile(r"[a-f0-9]{64}")
PREFIX = re.compile(r"[a-f0-9]{2,8}")
MANIFEST_FIELDS = {"schema_version", "kind", "recipient", "release_id", "sequence", "parent_release_id",
    "created_at", "taxonomy_version", "request_hash", "records_count", "image_count", "image_bytes",
    "withdrawal_sequence", "parts", "key_id", "signature"}
OBJECT_FIELDS = {"path", "sha256", "size", "mime", "width", "height"}


def enabled():
    raw = os.environ.get(ENABLE_ENV, "false").strip().lower()
    _require(raw in {"false", "true", "0", "1"}, "invalid snapshot v3 switch")
    return raw in {"true", "1"}


snapshot_v3_enabled = enabled


def image_budget():
    raw = os.environ.get("OMNISIGNAL_SNAPSHOT_V3_MAX_IMAGE_BYTES", "").strip()
    _require(not raw or raw.isascii() and raw.isdecimal() and len(raw) <= 12
             and 0 < int(raw) <= MAX_SNAPSHOT_IMAGE_BYTES, "invalid snapshot image budget")
    return int(raw) if raw else DEFAULT_IMAGE_BUDGET


def retained_budget():
    """All immutable v3 payloads, including historical/unreferenced content."""
    raw = os.environ.get("OMNISIGNAL_SNAPSHOT_V3_MAX_RETAINED_BYTES", "").strip()
    _require(not raw or raw.isascii() and raw.isdecimal() and len(raw) <= 12
             and 0 < int(raw) <= MAX_SNAPSHOT_IMAGE_BYTES, "invalid snapshot retained budget")
    return int(raw) if raw else DEFAULT_RETAINED_BUDGET


def storage(root):
    return _file(Path(root), "v3")


def retained_content_bytes(root):
    """Count real persisted payload bytes; no deletion or implied GC permission.

    Private SQLite journals, lock files and temporary staging remain protected
    by the separate disk-free gate, not falsely described as immutable payloads.
    """
    folder = storage(root)
    total = 0
    for category in ("objects", "parts", "releases"):
        directory = _file(folder, category)
        if directory.exists():
            _require(directory.is_dir(), "invalid snapshot content directory")
            for entry in directory.rglob("*"):
                path = _file(folder, entry.relative_to(folder).as_posix())
                if path.is_file():
                    total += path.stat().st_size
    return total


def _planned_content_bytes(root, category, sha, size):
    """Existing bytes already count in retained usage; only absent objects add."""
    path = _file(storage(root), category + "/" + _sha(sha))
    if path.exists():
        _require(path.is_file() and path.stat().st_size == size, "snapshot immutable object conflict")
        with path.open("rb") as stream:
            _require(hashlib.file_digest(stream, "sha256").hexdigest() == sha, "snapshot immutable object conflict")
        return 0
    return size


def _bounded(value, limit, name, minimum=0):
    _require(type(value) is int and minimum <= value <= limit, "invalid snapshot " + name)
    return value


def _sha(value):
    _require(isinstance(value, str) and HASH.fullmatch(value), "invalid snapshot hash")
    return value


def request_hash(records, recipient):
    return digest(canonical({"recipient": validate_library_recipient(recipient),
                             "records": sorted(records, key=lambda record: record["case_id"])}))


def _object_descriptor(value):
    _require(isinstance(value, dict) and set(value) == OBJECT_FIELDS, "invalid snapshot object descriptor")
    _sha(value["sha256"])
    _require(isinstance(value["path"], str) and re.fullmatch(r"images/" + value["sha256"] + r"\.(png|jpe?g|webp)", value["path"]),
             "snapshot image path must bind hash")
    _bounded(value["size"], MAX_IMAGE_BYTES, "image size", 1)
    _require(value["mime"] in {"image/png", "image/jpeg", "image/webp"}, "invalid snapshot image MIME")
    extension = value["path"].rsplit(".", 1)[1]
    _require(value["mime"] == {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}[extension],
             "snapshot image extension/MIME mismatch")
    _bounded(value["width"], MAX_IMAGE_PIXELS, "image width", 1)
    _bounded(value["height"], MAX_IMAGE_PIXELS, "image height", 1)
    _require(value["width"] * value["height"] <= MAX_IMAGE_PIXELS, "snapshot image pixel limit")
    return value


def validate_manifest(value):
    _require(isinstance(value, dict) and set(value) == MANIFEST_FIELDS, "invalid snapshot manifest fields")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 3 and value["kind"] == "snapshot",
             "invalid snapshot schema")
    validate_library_recipient(value["recipient"])
    _identifier(value["release_id"]); _integer(value["sequence"]); _identifier(value["key_id"])
    if value["parent_release_id"] is not None:
        _identifier(value["parent_release_id"])
        _require(value["parent_release_id"] != value["release_id"], "snapshot parent self reference")
    _text(value["taxonomy_version"], "taxonomy_version"); _sha(value["request_hash"])
    try:
        _require(isinstance(value["created_at"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", value["created_at"]),
                 "invalid snapshot timestamp")
        stamp = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        _require(stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0, "snapshot timestamp must be UTC")
    except (ValueError, TypeError, AttributeError) as exc:
        raise KnowledgeError("invalid snapshot timestamp") from exc
    _bounded(value["records_count"], MAX_SNAPSHOT_RECORDS, "record count")
    _bounded(value["image_count"], MAX_SNAPSHOT_IMAGES, "image count")
    _bounded(value["image_bytes"], MAX_SNAPSHOT_IMAGE_BYTES, "image bytes")
    _bounded(value["withdrawal_sequence"], 9007199254740991, "withdrawal sequence")
    parts = value["parts"]
    _require(isinstance(parts, list) and len(parts) <= MAX_PARTS, "snapshot part count limit")
    prefixes, hashes = [], set()
    for part in parts:
        _require(isinstance(part, dict) and set(part) == {"prefix", "sha256", "size", "records_count"}, "invalid snapshot part descriptor")
        _require(isinstance(part["prefix"], str) and PREFIX.fullmatch(part["prefix"]), "invalid snapshot prefix")
        _sha(part["sha256"])
        _bounded(part["size"], MAX_PART_BYTES, "part size", 1)
        _bounded(part["records_count"], MAX_PART_RECORDS, "part records", 1)
        _require(part["sha256"] not in hashes, "duplicate snapshot part hash")
        hashes.add(part["sha256"]); prefixes.append(part["prefix"])
    _require(prefixes == sorted(set(prefixes)) and all(not right.startswith(left) for left, right in zip(prefixes, prefixes[1:])),
             "snapshot prefixes overlap or are unsorted")
    _require(sum(part["records_count"] for part in parts) == value["records_count"], "snapshot record count mismatch")
    _require(len(canonical(value)) + 1 <= MAX_RECORD_BYTES, "snapshot manifest size limit")
    return value


def validate_part(raw, descriptor, manifest):
    _require(len(raw) == descriptor["size"] and len(raw) <= MAX_PART_BYTES and digest(raw) == descriptor["sha256"],
             "snapshot part hash/size mismatch")
    value = _decode(raw)
    _require(raw == canonical(value) + b"\n", "snapshot part must use canonical JSON and LF")
    _require(set(value) == {"schema_version", "prefix", "records", "objects"}
             and type(value["schema_version"]) is int and value["schema_version"] == 3
             and value["prefix"] == descriptor["prefix"], "invalid snapshot part")
    records, objects = value["records"], value["objects"]
    _require(isinstance(records, list) and len(records) == descriptor["records_count"] and len(records) <= MAX_PART_RECORDS,
             "snapshot part record count mismatch")
    ids, refs = [], set()
    for record in records:
        refs |= _record(record)
        _require(record["op"] == "upsert", "snapshot records must be upserts")
        _record_scope(record, manifest["recipient"])
        ids.append(record["case_id"])
        _require(digest(record["case_id"].encode()).startswith(descriptor["prefix"]), "snapshot case prefix mismatch")
    _require(ids == sorted(set(ids)), "snapshot duplicate or unsorted cases")
    _require(isinstance(objects, list) and len(objects) <= MAX_SNAPSHOT_IMAGES, "invalid snapshot objects")
    paths = []
    for entry in objects:
        _object_descriptor(entry); paths.append(entry["path"])
    _require(paths == sorted(set(paths)) and set(paths) == refs, "snapshot object references mismatch")
    return value


def _bytes(root, category, sha, limit):
    path = _file(storage(root), category + "/" + _sha(sha))
    _require(path.is_file() and path.stat().st_size <= limit, "missing/oversized snapshot content")
    data = path.read_bytes()
    _require(digest(data) == sha, "snapshot content hash mismatch")
    return data


def read_manifest(root, release_id, trusted_keys=None):
    path = _file(storage(root), "releases/" + _identifier(release_id) + "/manifest.json")
    value = _load(path)
    _require(path.read_bytes() == canonical(value) + b"\n", "snapshot manifest must use canonical JSON and LF")
    validate_manifest(value)
    _require(value["release_id"] == release_id, "snapshot release identity mismatch")
    if trusted_keys is not None:
        verify_signature(value, trusted_keys)
    return value


def read_snapshot_latest(root):
    folder = storage(root)
    if not _file(folder, "latest.json").exists():
        return None
    pointer = _load(_file(folder, "latest.json"))
    _require(set(pointer) == {"release_id", "sequence", "manifest_sha256"}, "invalid snapshot latest")
    _identifier(pointer["release_id"]); _integer(pointer["sequence"]); _sha(pointer["manifest_sha256"])
    path = _file(folder, "releases/" + pointer["release_id"] + "/manifest.json")
    manifest = read_manifest(root, pointer["release_id"])
    _require(manifest["sequence"] == pointer["sequence"] and digest(path.read_bytes()) == pointer["manifest_sha256"], "snapshot latest hash mismatch")
    return pointer


def validate_object(data, descriptor):
    _object_descriptor(descriptor)
    _require(len(data) == descriptor["size"] and digest(data) == descriptor["sha256"], "snapshot image hash/size mismatch")
    _image(data)
    with Image.open(io.BytesIO(data)) as image:
        mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
        _require(image.n_frames == 1 and mime == descriptor["mime"] and (image.width, image.height) == (descriptor["width"], descriptor["height"]),
                 "snapshot image metadata mismatch")


def snapshot_inventory(root, manifest, *, verify_images=False):
    """Read bounded parts one by one; never accumulate image bytes."""
    records, images, ids = [], {}, set()
    for descriptor in manifest["parts"]:
        part = validate_part(_bytes(root, "parts", descriptor["sha256"], MAX_PART_BYTES), descriptor, manifest)
        for record in part["records"]:
            _require(record["case_id"] not in ids, "snapshot duplicate case across parts")
            ids.add(record["case_id"]); records.append(record)
        for entry in part["objects"]:
            identity = {key: entry[key] for key in OBJECT_FIELDS - {"path"}}
            prior = images.get(entry["sha256"])
            _require(prior is None or prior == identity, "snapshot conflicting image descriptors")
            if prior is None and verify_images:
                validate_object(_bytes(root, "objects", entry["sha256"], MAX_IMAGE_BYTES), entry)
            images[entry["sha256"]] = identity
    _require(len(records) == manifest["records_count"] and len(images) == manifest["image_count"]
             and sum(entry["size"] for entry in images.values()) == manifest["image_bytes"], "snapshot inventory mismatch")
    return sorted(records, key=lambda record: record["case_id"]), images


def snapshot_cases(root):
    pointer = read_snapshot_latest(root)
    if pointer is None:
        return {}
    records, _ = snapshot_inventory(root, read_manifest(root, pointer["release_id"]))
    return {record["case_id"]: record["case"] for record in records}


def _put(root, category, data):
    sha = digest(data)
    path = _file(storage(root), category + "/" + sha)
    if path.exists():
        _require(path.is_file() and path.stat().st_size == len(data) and digest(path.read_bytes()) == sha, "snapshot immutable object conflict")
        return sha
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return sha


def _parts(records, objects):
    buckets = {}
    for record in records:
        buckets.setdefault(digest(record["case_id"].encode())[:2], []).append(record)
    part_count = 0
    def add(prefix, group):
        nonlocal part_count
        paths = {image["path"] for record in group for image in record["case"]["images"]}
        # Split before constructing a potentially hundreds-of-MiB JSON value.
        lower_bound = sum(len(canonical(record)) + 1 for record in group)
        raw = b""
        if len(group) <= TARGET_PART_RECORDS and lower_bound <= MAX_PART_BYTES:
            value = {"schema_version": 3, "prefix": prefix, "records": sorted(group, key=lambda record: record["case_id"]),
                     "objects": [objects[path] for path in sorted(paths)]}
            raw = canonical(value) + b"\n"
        if not raw or len(raw) > MAX_PART_BYTES:
            _require(len(prefix) < 8, "snapshot partition depth exhausted")
            children = {}
            for record in group:
                children.setdefault(digest(record["case_id"].encode())[:len(prefix)+1], []).append(record)
            for child in sorted(children):
                yield from add(child, children[child])
        else:
            part_count += 1
            _require(part_count <= MAX_PARTS, "snapshot part count limit")
            yield {"prefix": prefix, "sha256": digest(raw), "size": len(raw), "records_count": len(group)}, raw
    for prefix in sorted(buckets):
        yield from add(prefix, buckets[prefix])


def _history_db(folder):
    path = _file(folder, ".history.sqlite")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,content_hash TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS applied(release_id TEXT PRIMARY KEY,sequence INTEGER UNIQUE NOT NULL,manifest_hash TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS legacy_applied(release_id TEXT PRIMARY KEY,sequence INTEGER UNIQUE NOT NULL,manifest_hash TEXT NOT NULL)")
    return db


def _check_history(db, records):
    for record in records:
        old = db.execute("SELECT revision,content_hash FROM cases WHERE case_id=?", (record["case_id"],)).fetchone()
        _require(old is None or record["revision"] > old[0] or (record["revision"], record["content_hash"]) == old,
                 "snapshot case revision conflict/rollback")


def _remember(db, manifest, records, manifest_hash):
    _check_history(db, records)
    db.executemany("INSERT INTO cases VALUES(?,?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision,content_hash=excluded.content_hash WHERE cases.revision!=excluded.revision OR cases.content_hash!=excluded.content_hash",
                   [(record["case_id"], record["revision"], record["content_hash"]) for record in records])
    db.execute("INSERT INTO applied VALUES(?,?,?)", (manifest["release_id"], manifest["sequence"], manifest_hash))
    db.commit()


def _reconcile(root, db, keys, recipient):
    # Seed retained revision high-water marks, including cases omitted by old
    # full releases. Verified legacy content is never copied into v3 storage here.
    for path in sorted(_release_manifests(root), key=lambda item: _load(item)["sequence"]):
        manifest = _load(path); verify_signature(manifest, keys)
        _require(envelope_recipient(manifest) == recipient, "snapshot legacy recipient mismatch")
        sha = digest(path.read_bytes())
        prior = db.execute("SELECT manifest_hash FROM legacy_applied WHERE release_id=?", (manifest["release_id"],)).fetchone()
        if prior is not None:
            _require(prior[0] == sha, "snapshot immutable legacy manifest conflict")
        else:
            _, records = verify_release(path.parent, keys)
            _check_history(db, records)
            db.executemany("INSERT INTO cases VALUES(?,?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision,content_hash=excluded.content_hash",
                           [(record["case_id"], record["revision"], record["content_hash"]) for record in records])
            db.execute("INSERT INTO legacy_applied VALUES(?,?,?)", (manifest["release_id"], manifest["sequence"], sha))
            db.commit()
    manifests = []
    folder = _file(storage(root), "releases")
    if folder.exists():
        for entry in folder.iterdir():
            if entry.is_dir():
                manifest = read_manifest(root, entry.name, keys)
                _require(manifest["recipient"] == recipient, "snapshot recipient mismatch")
                manifests.append(manifest)
    for manifest in sorted(manifests, key=lambda value: value["sequence"]):
        raw = _file(folder, manifest["release_id"] + "/manifest.json").read_bytes()
        old = db.execute("SELECT manifest_hash FROM applied WHERE release_id=?", (manifest["release_id"],)).fetchone()
        if old is not None:
            _require(old[0] == digest(raw), "snapshot immutable manifest conflict")
        else:
            records, _ = snapshot_inventory(root, manifest, verify_images=True)
            _remember(db, manifest, records, digest(raw))
    return manifests


def publish_snapshot(*, root, release_id, records, asset_sources, private_key, key_id, recipient,
                     requested_records=None, taxonomy_version="nova-1", before_sign=None):
    """Publish a complete catalog; callers freeze the requested delta separately.

    asset_sources maps signed image paths to local files. Historical images can
    omit a source only when the identical verified CAS object already exists.
    """
    root = Path(root); folder = storage(root)
    _identifier(release_id); _identifier(key_id); recipient = validate_library_recipient(recipient)
    _require(isinstance(records, list) and len(records) <= MAX_SNAPSHOT_RECORDS, "snapshot record count limit")
    # Freeze canonical values before callbacks or a second partition pass; caller
    # mutation must not change the signed catalog after its request was frozen.
    frozen_records = []
    for record in records:
        _record(record)
        frozen_records.append(_decode(canonical(record)))
    records = sorted(frozen_records, key=lambda value: value["case_id"])
    asset_sources = {name: Path(path) for name, path in asset_sources.items()}
    ids, paths = set(), set()
    for record in records:
        paths |= _record(record); _record_scope(record, recipient)
        _require(record["op"] == "upsert" and record["case_id"] not in ids, "snapshot duplicate or non-upsert record")
        ids.add(record["case_id"])
    requested = records if requested_records is None else requested_records
    _require(isinstance(requested, list) and len(requested) <= MAX_SNAPSHOT_RECORDS, "invalid frozen snapshot request")
    by_id = {record["case_id"]: record for record in records}
    requested_ids = set()
    for record in requested:
        _record(record); _record_scope(record, recipient)
        _require(record["op"] == "upsert" and by_id.get(record["case_id"]) == record and record["case_id"] not in requested_ids,
                 "frozen snapshot request not present in catalog")
        requested_ids.add(record["case_id"])
    frozen_hash = request_hash(requested, recipient)
    keys = {key_id: private_key.public_key()}
    with _lock(folder), closing(_history_db(folder)) as db:
        manifests = _reconcile(root, db, keys, recipient)
        previous = read_snapshot_latest(root)
        legacy = read_latest(root)
        known = next((value for value in manifests if value["release_id"] == release_id), None)
        legacy_high = db.execute("SELECT COALESCE(MAX(sequence),0) FROM legacy_applied").fetchone()[0]
        highest = max([value["sequence"] for value in manifests] + [legacy_high])
        if known is not None:
            prior_records, _ = snapshot_inventory(root, known, verify_images=True)
            _require(known["request_hash"] == frozen_hash and prior_records == records and known["taxonomy_version"] == taxonomy_version,
                     "snapshot recovery content conflict")
            if before_sign is not None:
                before_sign()
            if known["sequence"] == highest and (previous is None or previous["sequence"] < highest):
                _atomic_json(_file(folder, "latest.json"), {"release_id": release_id, "sequence": known["sequence"],
                    "manifest_sha256": digest(_file(folder, "releases/" + release_id + "/manifest.json").read_bytes())})
            return known
        newest_snapshot = max(manifests, key=lambda value: value["sequence"], default=None)
        _require(newest_snapshot is None or previous is not None and previous["sequence"] == newest_snapshot["sequence"],
                 "snapshot prior publication recovery required")
        _require(newest_snapshot is None or legacy_high < newest_snapshot["sequence"],
                 "snapshot legacy publication advanced; explicit rebaseline required")
        _check_history(db, records)
        withdrawn, withdrawal_sequence = set(), 0
        for path in _withdrawal_files(root):
            value = _load(path); verify_signature(value, keys)
            _require(envelope_recipient(value) == recipient, "snapshot withdrawal scope mismatch")
            withdrawn.update(value["case_ids"]); withdrawal_sequence = max(withdrawal_sequence, value["sequence"])
        _require(not ids.intersection(withdrawn), "withdrawn case cannot be republished")
        objects, distinct, object_sources, image_bytes, additional_bytes = {}, {}, {}, 0, 0
        for name in sorted(paths):
            match = re.fullmatch(r"images/([a-f0-9]{64})\.(png|jpe?g|webp)", name)
            _require(match is not None, "snapshot image path must bind hash")
            sha = match.group(1)
            target = _file(folder, "objects/" + sha)
            source = Path(asset_sources[name]) if name in asset_sources else target
            _require(not source.is_symlink() and source.is_file() and source.stat().st_size <= MAX_IMAGE_BYTES, "missing/oversized snapshot image")
            data = source.read_bytes(); _require(digest(data) == sha, "snapshot image source hash mismatch")
            if sha not in distinct:
                _image(data)
                with Image.open(io.BytesIO(data)) as image:
                    _require(image.n_frames == 1, "snapshot animated image forbidden")
                    descriptor = {"sha256": sha, "size": len(data), "mime": {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format],
                                  "width": image.width, "height": image.height}
                distinct[sha] = descriptor
                object_sources[sha] = source
                image_bytes += len(data)
                _require(image_bytes <= image_budget() and len(distinct) <= MAX_SNAPSHOT_IMAGES,
                         "snapshot image capacity exceeded")
                additional_bytes += _planned_content_bytes(root, "objects", sha, len(data))
            objects[name] = {"path": name, **distinct[sha]}
        parts = []
        # First pass measures exact new parts without writing any CAS payload.
        # Only descriptors are retained; serialization stays bounded per part.
        for descriptor, raw in _parts(records, objects):
            validate_part(raw, descriptor, {"recipient": recipient})
            additional_bytes += _planned_content_bytes(root, "parts", descriptor["sha256"], descriptor["size"])
            parts.append(descriptor)
        payload = {"schema_version": 3, "kind": "snapshot", "recipient": recipient, "release_id": release_id,
            "sequence": highest + 1, "parent_release_id": previous["release_id"] if previous else legacy["release_id"] if legacy else None,
            "created_at": utc_stamp(), "taxonomy_version": taxonomy_version, "request_hash": frozen_hash,
            "records_count": len(records), "image_count": len(distinct), "image_bytes": image_bytes,
            "withdrawal_sequence": withdrawal_sequence, "parts": parts}
        envelope = {**payload, "key_id": key_id, "signature": "A" * 88}
        validate_manifest(envelope)
        manifest_bytes = len(canonical(envelope)) + 1
        _require(retained_content_bytes(root) + additional_bytes + manifest_bytes <= retained_budget(),
                 "snapshot retained capacity exceeded")
        _require(shutil.disk_usage(folder).free > additional_bytes + manifest_bytes + MAX_PART_BYTES + MAX_RECORD_BYTES,
                 "snapshot disk capacity unavailable")
        for sha, source in object_sources.items():
            data = source.read_bytes()
            _require(len(data) == distinct[sha]["size"] and digest(data) == sha, "snapshot image changed after capacity preflight")
            _put(root, "objects", data)
        # Second pass writes the identical frozen parts, one at a time.
        for expected, (descriptor, raw) in zip(parts, _parts(records, objects), strict=True):
            _require(descriptor == expected, "snapshot catalog changed after capacity preflight")
            _put(root, "parts", raw)
        snapshot_inventory(root, payload, verify_images=True)
        if before_sign is not None:
            before_sign()
        _require(retained_content_bytes(root) + manifest_bytes <= retained_budget(),
                 "snapshot retained capacity exceeded")
        signed = _signed(payload, private_key, key_id)
        releases = _file(folder, "releases"); releases.mkdir(exist_ok=True)
        target = _file(folder, "releases/" + release_id)
        stage = Path(tempfile.mkdtemp(prefix=".ready-", dir=folder))
        try:
            _atomic_json(stage / "manifest.json", signed)
            stage.rename(target)
            manifest_hash = digest((target / "manifest.json").read_bytes())
            _remember(db, signed, records, manifest_hash)
            _atomic_json(_file(folder, "latest.json"), {"release_id": release_id, "sequence": signed["sequence"], "manifest_sha256": manifest_hash})
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return signed


def referenced_file(root, release_id, category, sha):
    """Authorize only hashes authenticated by the named complete release."""
    _sha(sha); _require(category in {"parts", "objects"}, "invalid snapshot file category")
    manifest = read_manifest(root, release_id)
    for descriptor in manifest["parts"]:
        if category == "parts" and descriptor["sha256"] == sha:
            validate_part(_bytes(root, category, sha, MAX_PART_BYTES), descriptor, manifest)
            return _file(storage(root), category + "/" + sha)
        if category == "objects":
            part = validate_part(_bytes(root, "parts", descriptor["sha256"], MAX_PART_BYTES), descriptor, manifest)
            for obj in part["objects"]:
                if obj["sha256"] == sha:
                    validate_object(_bytes(root, category, sha, MAX_IMAGE_BYTES), obj)
                    return _file(storage(root), category + "/" + sha)
    raise KnowledgeError("snapshot file not referenced by release")
