"""Offline publisher. No crawling, model invocation or network write endpoint.

Signatures cover canonical UTF-8 JSON without the ``signature`` field. Trust keys
are supplied separately; neither manifests nor a discovery pointer establish trust.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import warnings
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from PIL import Image

MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORDS = 2000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KINDS = {"author_statement", "media_statement", "visual_observation", "analysis_inference"}


class KnowledgeError(ValueError):
    """A package is invalid or conflicts with previously published state."""


def canonical(value: object) -> bytes:
    # This deliberately narrow JSON profile matches the Nova JavaScript verifier.
    # Floats (including 1.0) and unsafe integers cannot preserve signed bytes in JS.
    def validate(item):
        if item is None or type(item) in {str, bool}:
            return
        if type(item) is int:
            _require(abs(item) <= 9007199254740991, "canonical integer exceeds JavaScript safe range")
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict:
            _require(all(type(key) is str for key in item), "canonical object keys must be strings")
            for child in item.values():
                validate(child)
            return
        raise KnowledgeError("canonical JSON supports only strings, booleans, null, arrays, objects and safe integers")
    validate(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def knowledge_root() -> Path:
    return Path(os.environ.get("OMNISIGNAL_KNOWLEDGE_DIR", "/app/data/knowledge"))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KnowledgeError(message)


def _identifier(value: object) -> str:
    _require(isinstance(value, str) and bool(ID.fullmatch(value)), "invalid stable identifier")
    return value


def _integer(value: object) -> int:
    _require(type(value) is int and value >= 1, "sequence/revision must be a positive integer")
    return value


def _relative(value: object) -> str:
    _require(isinstance(value, str) and bool(value) and "\\" not in value and ":" not in value, "invalid relative path")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and all(p not in {"", ".", ".."} for p in value.split("/")), "unsafe relative path")
    return str(path)


def _file(root: Path, relative: str) -> Path:
    relative = _relative(relative)
    _require(not root.is_symlink(), "symlink root forbidden")
    candidate = root.joinpath(*relative.split("/"))
    for part in (candidate, *candidate.parents):
        if part == root.parent:
            break
        _require(not part.is_symlink(), "symlink forbidden")
    _require(candidate.resolve().is_relative_to(root.resolve()), "path escapes root")
    return candidate


def _decode(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(KnowledgeError("non-finite JSON number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeError("invalid JSON") from exc
    _require(isinstance(value, dict), "JSON object required")
    return value


def _load(path: Path, limit: int = MAX_RECORD_BYTES) -> dict:
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit, "missing or oversized JSON")
    return _decode(path.read_bytes())


def _text(value: object, name: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 20000, f"missing/invalid {name}")


def validate_case(case: dict) -> set[str]:
    _require(isinstance(case, dict), "case must be an object")
    _identifier(case.get("case_id"))
    _integer(case.get("revision"))
    for key in ("title", "summary_zh", "brand_or_team", "analysis_version", "model_version"):
        _text(case.get(key), key)
    _require(bool(re.search(r"[\u3400-\u9fff]", case["summary_zh"])), "Chinese summary required")
    _require(case.get("review_status") == "approved", "human review required")
    _require(case.get("display_permission") == "allowed", "case display permission required")
    tags = case.get("tags", {})
    _require(isinstance(tags, dict), "invalid tags")
    for field in ("industry", "medium"):
        _require(isinstance(tags.get(field), list) and bool(tags[field]), f"{field} tags required")
        for tag in tags[field]:
            _text(tag, field)
    sources, images, claims = (case.get(key) for key in ("sources", "images", "claims"))
    for value in (sources, images, claims):
        _require(isinstance(value, list) and 0 < len(value) <= 500, "sources/images/claims required and bounded")
    def identifiers(values, field):
        _require(all(isinstance(v, dict) for v in values), "entity must be an object")
        ids = [_identifier(v.get(field)) for v in values]
        _require(len(ids) == len(set(ids)), f"duplicate {field}")
        return set(ids)
    source_ids = identifiers(sources, "source_id")
    image_ids = identifiers(images, "image_id")
    identifiers(claims, "claim_id")
    for source in sources:
        _require(source.get("display_permission") == "allowed", "source permission unknown/denied")
        _require(isinstance(source.get("url"), str) and len(source["url"]) <= 8192, "source URL required")
        try:
            parsed = urlsplit(source["url"])
            _require(parsed.scheme in {"https", "http"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None, "invalid source URL")
        except ValueError as exc:
            raise KnowledgeError("invalid source URL") from exc
    paths = set()
    for image in images:
        _require(image.get("display_permission") == "allowed", "image permission unknown/denied")
        _require(image.get("source_id") in source_ids, "image references unknown source")
        path = _relative(image.get("path"))
        _require(path.startswith("images/"), "images must use images/ namespace")
        paths.add(path)
    for claim in claims:
        _text(claim.get("text"), "claim text")
        _require(claim.get("kind") in KINDS, "invalid claim evidence kind")
        _require("source_ids" in claim and "image_ids" in claim, "claim requires explicit source_ids and image_ids arrays")
        source_refs, image_refs = claim["source_ids"], claim["image_ids"]
        _require(isinstance(source_refs, list) and isinstance(image_refs, list), "invalid evidence references")
        _require(bool(source_refs or image_refs), "claim requires evidence")
        _require(all(isinstance(v, str) and v in source_ids for v in source_refs), "unknown claim source")
        _require(all(isinstance(v, str) and v in image_ids for v in image_refs), "unknown claim image")
    return paths


def _image(data: bytes) -> None:
    _require(0 < len(data) <= MAX_IMAGE_BYTES, "image size limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                _require(image.format in {"JPEG", "PNG", "WEBP"}, "unsupported image format")
                _require(image.width * image.height <= MAX_IMAGE_PIXELS, "image pixel limit")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except KnowledgeError:
        raise
    except Exception as exc:
        raise KnowledgeError("image decode failed") from exc


def _signed(payload: dict, key: Ed25519PrivateKey, key_id: str) -> dict:
    result = {**payload, "key_id": _identifier(key_id)}
    result["signature"] = base64.b64encode(key.sign(canonical(result))).decode("ascii")
    return result


def verify_signature(payload: dict, trusted_keys: dict[str, Ed25519PublicKey]) -> None:
    key = trusted_keys.get(_identifier(payload.get("key_id")))
    _require(isinstance(key, Ed25519PublicKey), "untrusted signing key")
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    try:
        key.verify(base64.b64decode(payload["signature"], validate=True), canonical(unsigned))
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise KnowledgeError("invalid signature") from exc


def _record(record: dict) -> set[str]:
    _require(isinstance(record, dict), "record must be an object")
    _identifier(record.get("case_id"))
    _integer(record.get("revision"))
    if record.get("op") == "upsert":
        case = record.get("case")
        paths = validate_case(case)
        _require(case["case_id"] == record["case_id"] and case["revision"] == record["revision"], "record identity mismatch")
        hashed = case
    else:
        _require(record.get("op") == "delete" and "case" not in record, "invalid record operation")
        paths = set()
        hashed = {k: record[k] for k in ("op", "case_id", "revision")}
    _require(record.get("content_hash") == digest(canonical(hashed)), "record content hash mismatch")
    _require(len(canonical(record)) <= MAX_RECORD_BYTES, "record too large")
    return paths


def make_record(case: dict) -> dict:
    validate_case(case)
    case = json.loads(canonical(case))
    return {"op": "upsert", "case_id": case["case_id"], "revision": case["revision"], "content_hash": digest(canonical(case)), "case": case}


def delete_record(case_id: str, revision: int) -> dict:
    value = {"op": "delete", "case_id": _identifier(case_id), "revision": _integer(revision)}
    return {**value, "content_hash": digest(canonical(value))}


def verify_release(directory: Path | str, trusted_keys: dict[str, Ed25519PublicKey]) -> tuple[dict, list[dict]]:
    directory = Path(directory)
    manifest = _load(_file(directory, "manifest.json"))
    verify_signature(manifest, trusted_keys)
    _require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "unsupported schema")
    _identifier(manifest.get("release_id"))
    _integer(manifest.get("sequence"))
    _text(manifest.get("created_at"), "created_at")
    try:
        timestamp = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
        _require(timestamp.utcoffset() is not None and timestamp.utcoffset().total_seconds() == 0, "created_at must be UTC")
    except ValueError as exc:
        raise KnowledgeError("invalid created_at") from exc
    _text(manifest.get("taxonomy_version"), "taxonomy_version")
    _require(manifest.get("kind") in {"full", "delta"}, "invalid release kind")
    parent = manifest.get("parent_release_id")
    _require((manifest["kind"] == "full" and parent is None) or (manifest["kind"] == "delta" and isinstance(parent, str) and bool(ID.fullmatch(parent))), "invalid parent")
    files = manifest.get("files")
    _require(isinstance(files, list) and 1 <= len(files) <= MAX_RECORDS * 500 + 1, "invalid file inventory")
    names, total = set(), 0
    for entry in files:
        _require(isinstance(entry, dict), "invalid file entry")
        name = _relative(entry.get("path"))
        _require(name == "records.jsonl" or name.startswith("images/"), "unexpected file namespace")
        _require(name not in names, "duplicate file")
        names.add(name)
        size = entry.get("size")
        _require(type(size) is int and size >= 0, "invalid file size")
        total += size
        _require(total <= MAX_PACKAGE_BYTES, "package size limit")
        path = _file(directory, name)
        _require(path.is_file() and path.stat().st_size == size, "missing/file size mismatch")
        data = path.read_bytes()
        _require(digest(data) == entry.get("sha256"), "file hash mismatch")
        if name != "records.jsonl":
            _image(data)
    _require("records.jsonl" in names, "records inventory missing")
    actual = set()
    for path in directory.rglob("*"):
        _require(not path.is_symlink(), "symlink in release")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    _require(actual == names | {"manifest.json"}, "unlisted file in release")
    records, identities, refs = [], set(), set()
    with _file(directory, "records.jsonl").open("rb") as stream:
        while line := stream.readline(MAX_RECORD_BYTES + 2):
            _require(len(line) <= MAX_RECORD_BYTES + 1, "oversized record")
            record = _decode(line)
            refs |= _record(record)
            _require(record["case_id"] not in identities, "duplicate case in release")
            identities.add(record["case_id"])
            records.append(record)
            _require(len(records) <= MAX_RECORDS, "too many records")
    _require(refs == names - {"records.jsonl"}, "missing or unreferenced images")
    _require(type(manifest.get("records_count")) is int and manifest["records_count"] == len(records), "record count mismatch")
    return manifest, records


def read_latest(root: Path | str | None = None) -> dict | None:
    root = Path(root) if root is not None else knowledge_root()
    path = _file(root, "latest.json")
    if not path.exists():
        return None
    value = _load(path)
    _identifier(value.get("release_id"))
    _integer(value.get("sequence"))
    return value


def _atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    path = _file(root, ".publish.lock")
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise KnowledgeError("publisher is locked") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise KnowledgeError("publisher is locked") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _release_manifests(root: Path) -> list[Path]:
    directory = _file(root, "releases")
    if not directory.exists():
        return []
    manifests = []
    for entry in directory.iterdir():
        release = _file(root, f"releases/{entry.name}")
        if release.is_dir():
            manifests.append(_file(root, f"releases/{entry.name}/manifest.json"))
    return manifests


def _withdrawal_files(root: Path) -> list[Path]:
    directory = _file(root, "withdrawals")
    if not directory.exists():
        return []
    return [_file(root, f"withdrawals/{path.name}") for path in directory.glob("*.json")]


def _history(root: Path) -> dict[str, tuple[dict, dict[str, str]]]:
    """Keep record and signed image fingerprints across full-snapshot omissions."""
    state = {}
    directories = _release_manifests(root)
    for path in sorted(directories, key=lambda p: _load(p)["sequence"]):
        manifest = _load(path)
        file_hashes = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
        for line in _file(root, f"releases/{path.parent.name}/records.jsonl").read_bytes().splitlines():
            record = _decode(line)
            refs = _record(record)
            _require(refs.issubset(file_hashes), "historical image inventory missing")
            state[record["case_id"]] = (record, {name: file_hashes[name] for name in refs})
    return state


def _withdrawn(root: Path) -> set[str]:
    result = set()
    for path in _withdrawal_files(root):
        result.update(_load(path).get("case_ids", []))
    return result


def materialize_release(root: Path | str, release_id: str | None = None) -> dict[str, dict]:
    """Read publisher-owned local state, not a substitute for consumer verification.

    A consumer must verify every downloaded manifest against its independent trust
    store and persist its sequence high-water mark. The API uses this helper only
    for local operational counts; withdrawals apply across every release version.
    """
    root = Path(root)
    if release_id is None:
        pointer = read_latest(root)
        if pointer is None:
            return {}
        release_id = pointer["release_id"]
    chain, visited = [], set()
    child_sequence = None
    while release_id is not None:
        _identifier(release_id)
        _require(release_id not in visited, "cyclic parent chain")
        visited.add(release_id)
        directory = _file(root, f"releases/{release_id}")
        manifest = _load(_file(directory, "manifest.json"))
        _require(manifest.get("release_id") == release_id, "manifest release identity mismatch")
        sequence = _integer(manifest.get("sequence"))
        _require(child_sequence is None or sequence == child_sequence - 1, "broken parent sequence")
        child_sequence = sequence
        chain.append(directory)
        _require(manifest.get("kind") in {"full", "delta"}, "invalid release kind")
        release_id = manifest.get("parent_release_id") if manifest["kind"] == "delta" else None
        _require(manifest["kind"] != "delta" or release_id is not None, "delta parent missing")
    cases = {}
    for directory in reversed(chain):
        with _file(directory, "records.jsonl").open("rb") as stream:
            while line := stream.readline(MAX_RECORD_BYTES + 2):
                _require(len(line) <= MAX_RECORD_BYTES + 1, "oversized record")
                record = _decode(line)
                _record(record)
                if record["op"] == "delete":
                    cases.pop(record["case_id"], None)
                else:
                    cases[record["case_id"]] = record["case"]
    for case_id in _withdrawn(root):
        cases.pop(case_id, None)
    return cases


def publish_release(*, root: Path | str, release_id: str, sequence: int, records: list[dict], asset_root: Path | str, private_key: Ed25519PrivateKey, key_id: str, kind: str = "full", parent_release_id: str | None = None, taxonomy_version: str = "1", created_at: str | None = None) -> dict:
    root, asset_root = Path(root), Path(asset_root)
    _identifier(release_id)
    _integer(sequence)
    _require(kind in {"full", "delta"}, "invalid release kind")
    _require(isinstance(records, list) and len(records) <= MAX_RECORDS, "invalid records")
    refs, ids = set(), set()
    for record in records:
        refs |= _record(record)
        _require(record["case_id"] not in ids, "duplicate case in release")
        ids.add(record["case_id"])
    with _lock(root):
        releases = _file(root, "releases")
        releases.mkdir(exist_ok=True)
        target = _file(root, f"releases/{release_id}")
        if target.exists():
            old, old_records = verify_release(target, {key_id: private_key.public_key()})
            _require(old_records == records and all(old[k] == v for k, v in {"sequence": sequence, "kind": kind, "parent_release_id": parent_release_id, "taxonomy_version": taxonomy_version}.items()), "release ID content conflict")
            for name in refs:
                source = _file(asset_root, name)
                _require(source.is_file() and source.stat().st_size <= MAX_IMAGE_BYTES, "missing/oversized image")
                _require(digest(source.read_bytes()) == digest(_file(target, name).read_bytes()), "release image conflict")
            # Recover an interruption between immutable directory rename and pointer
            # replacement. Repeating an older release must never roll the pointer back.
            latest = read_latest(root)
            if latest is None or latest["sequence"] < sequence:
                highest = max(_load(p)["sequence"] for p in _release_manifests(root))
                if sequence == highest:
                    _atomic_json(root / "latest.json", {"release_id": release_id, "sequence": sequence, "manifest_sha256": digest((target / "manifest.json").read_bytes())})
            return old
        latest = read_latest(root)
        previous_sequences = [_load(p)["sequence"] for p in _release_manifests(root)]
        _require(sequence == max(previous_sequences, default=0) + 1, "release sequence must be consecutive")
        if kind == "delta":
            _require(latest is not None and parent_release_id == latest["release_id"], "delta parent must be active release")
            _require(latest["sequence"] == sequence - 1, "delta parent sequence mismatch")
        else:
            _require(parent_release_id is None, "full release has no parent")
        withdrawn = _withdrawn(root)
        history = _history(root)
        for record in records:
            _require(record["op"] == "delete" or record["case_id"] not in withdrawn, "withdrawn case cannot be republished")
            historical = history.get(record["case_id"])
            if historical:
                previous, _ = historical
                _require(record["revision"] > previous["revision"] or record == previous, "case revision conflict/rollback")
        stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
        try:
            raw = b"".join(canonical(r) + b"\n" for r in records)
            _require(len(raw) <= MAX_PACKAGE_BYTES, "package size limit")
            (stage / "records.jsonl").write_bytes(raw)
            for name in sorted(refs):
                source = _file(asset_root, name)
                _require(source.is_file() and source.stat().st_size <= MAX_IMAGE_BYTES, "missing/oversized image")
                data = source.read_bytes()
                _image(data)
                dest = _file(stage, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            files = [{"path": path.relative_to(stage).as_posix(), "size": path.stat().st_size, "sha256": digest(path.read_bytes())} for path in sorted(stage.rglob("*")) if path.is_file()]
            file_hashes = {entry["path"]: entry["sha256"] for entry in files}
            for record in records:
                historical = history.get(record["case_id"])
                if historical and record["revision"] == historical[0]["revision"]:
                    # Compare the actual staged bytes, not an earlier read of the
                    # mutable asset directory. Paths alone are not evidence identity.
                    previous_hashes = historical[1]
                    current_hashes = {name: file_hashes[name] for name in _record(record)}
                    _require(current_hashes == previous_hashes, "case image evidence changed; increase revision")
            payload = {"schema_version": 1, "release_id": release_id, "sequence": sequence, "kind": kind, "parent_release_id": parent_release_id, "created_at": created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "taxonomy_version": taxonomy_version, "records_count": len(records), "files": files}
            manifest = _signed(payload, private_key, key_id)
            _atomic_json(stage / "manifest.json", manifest)
            verify_release(stage, {key_id: private_key.public_key()})
            stage.rename(target)
            _atomic_json(root / "latest.json", {"release_id": release_id, "sequence": sequence, "manifest_sha256": digest((target / "manifest.json").read_bytes())})
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def publish_withdrawals(*, root: Path | str, sequence: int, case_ids: list[str], private_key: Ed25519PrivateKey, key_id: str) -> dict:
    root = Path(root)
    _integer(sequence)
    _require(isinstance(case_ids, list) and bool(case_ids) and len(case_ids) <= MAX_RECORDS, "withdrawal cases required")
    ids = sorted({_identifier(case_id) for case_id in case_ids})
    with _lock(root):
        directory = _file(root, "withdrawals")
        directory.mkdir(exist_ok=True)
        target = _file(root, f"withdrawals/{sequence}.json")
        if target.exists():
            old = _load(target)
            verify_signature(old, {key_id: private_key.public_key()})
            preceding = set()
            for path in _withdrawal_files(root):
                if int(path.stem) < sequence:
                    preceding.update(_load(path)["case_ids"])
            _require(set(ids) | preceding == set(old["case_ids"]), "withdrawal sequence conflict")
            return old
        previous = [int(p.stem) for p in _withdrawal_files(root)]
        _require(sequence == max(previous, default=0) + 1, "withdrawal sequence must be consecutive")
        cumulative = sorted(_withdrawn(root) | set(ids))
        payload = _signed({"schema_version": 1, "sequence": sequence, "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "case_ids": cumulative}, private_key, key_id)
        _require(len(canonical(payload)) <= MAX_RECORD_BYTES, "withdrawal inventory too large")
        _atomic_json(target, payload)
        return payload


def load_private_key(path: Path | str, root: Path | str) -> Ed25519PrivateKey:
    path, root = Path(path), Path(root)
    _require(not path.resolve().is_relative_to(root.resolve()), "signing private key must be outside publication root")
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 16384, "invalid private key file")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    _require(isinstance(key, Ed25519PrivateKey), "Ed25519 key required")
    return key
