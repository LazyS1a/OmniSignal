"""Offline B-side reference receiver, not a production consumer integration.

No network, models, credentials, HTTP handlers or tenant access are implemented.
Immutable signed packages live locally; SQLite transactions select the active
logical view. A process lock serializes readers, writers and withdrawals. A
production BFF must additionally enforce workspace permissions on every read.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .release import (
    KnowledgeError, MAX_RECORD_BYTES, _file, _identifier, _integer, _lock,
    _require, canonical, digest, verify_release, verify_signature,
)


class KnowledgeReplica:
    """Local protocol acceptance harness with durable anti-replay state.

All public operations use the same nonblocking cross-process lock. Contention
raises KnowledgeError so an eventual worker can retry. Never share its root
with a publisher or let untrusted users write the replica directory.
"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        with _lock(self.root), self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS releases (
                    id TEXT PRIMARY KEY, sequence INTEGER UNIQUE NOT NULL,
                    manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cases (
                    release_id TEXT NOT NULL, case_id TEXT NOT NULL,
                    body TEXT NOT NULL, asset_release TEXT NOT NULL,
                    PRIMARY KEY (release_id, case_id));
                CREATE TABLE IF NOT EXISTS history (
                    case_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    record_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS withdrawals (
                    case_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS withdrawal_documents (
                    sequence INTEGER PRIMARY KEY, document_hash TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(_file(self.root, "replica.sqlite3"), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get(db, key: str, default: str = "") -> str:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set(db, key: str, value: object) -> None:
        db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    @staticmethod
    def _write(path: Path, body: bytes) -> None:
        with path.open("wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        # Windows does not expose directory fsync through this API. Files and
        # SQLite still use full flushes; POSIX additionally persists renames.
        if os.name != "nt":
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def accept_release(self, package_path: Path | str,
                       trusted_keys: dict[str, Ed25519PublicKey]) -> dict:
        """Validate, stage, materialize and atomically activate a release.

        First contact requires a full snapshot (which may start above sequence
        one). Later releases must be consecutive. After explicit rollback, new
        deltas still extend the highest *accepted* release, not the old active
        pointer. An idempotent old replay never changes the active pointer.
        """
        source = Path(package_path)
        with _lock(self.root), self._db() as db:
            manifest, records = verify_release(source, trusted_keys)
            release_id, sequence = manifest["release_id"], manifest["sequence"]
            manifest_hash = digest(canonical(manifest))
            old = db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
            target = _file(self.root, f"releases/{release_id}")
            if old:
                _require(old["manifest_hash"] == manifest_hash, "release ID content conflict")
                local, _ = verify_release(target, trusted_keys)
                _require(digest(canonical(local)) == manifest_hash, "local immutable release corrupted")
                return {"release_id": release_id, "status": "already_accepted", "active_release": self._get(db, "active")}

            high = int(self._get(db, "high_sequence", "0"))
            _require(sequence > high, "release sequence replay/out of order")
            _require(not high or sequence == high + 1, "release sequence gap")
            if manifest["kind"] == "delta":
                _require(high > 0 and manifest["parent_release_id"] == self._get(db, "high_release"), "delta parent mismatch or missing")
            else:
                _require(manifest["parent_release_id"] is None, "full release must not have a parent")

            for record in records:
                prior = db.execute("SELECT revision,record_hash FROM history WHERE case_id=?", (record["case_id"],)).fetchone()
                if prior:
                    _require(record["revision"] > prior["revision"] or
                             (record["revision"] == prior["revision"] and digest(canonical(record)) == prior["record_hash"]),
                             "case revision conflict/rollback")
                    if record["op"] == "upsert" and record["revision"] == prior["revision"]:
                        previous_case = db.execute("SELECT c.body,c.asset_release FROM cases c JOIN releases r ON r.id=c.release_id WHERE c.case_id=? ORDER BY r.sequence DESC LIMIT 1", (record["case_id"],)).fetchone()
                        _require(previous_case is not None, "case asset history missing")
                        previous_manifest = json.loads(db.execute("SELECT manifest FROM releases WHERE id=?", (previous_case["asset_release"],)).fetchone()[0])
                        def image_hashes(case, release_manifest):
                            inventory = {entry["path"]: entry["sha256"] for entry in release_manifest["files"]}
                            return {image["image_id"]: inventory[image["path"]] for image in case["images"]}
                        _require(image_hashes(record["case"], manifest) == image_hashes(json.loads(previous_case["body"]), previous_manifest),
                                 "case revision image content conflict")

            stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
            try:
                # Copy only the already bounded signed inventory, then validate
                # the copy again. Never activate paths back into source/A.
                self._write(stage / "manifest.json", canonical(manifest))
                for entry in manifest["files"]:
                    original = _file(source, entry["path"])
                    with original.open("rb") as stream:
                        body = stream.read(entry["size"] + 1)
                    _require(len(body) == entry["size"], "source changed while staging")
                    destination = _file(stage, entry["path"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._write(destination, body)
                copied, copied_records = verify_release(stage, trusted_keys)
                _require(copied == manifest and copied_records == records, "staging changed")
                for directory in sorted((p for p in stage.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    self._sync_directory(directory)
                self._sync_directory(stage)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # Recover a crash between immutable rename and DB commit.
                    orphan, _ = verify_release(target, trusted_keys)
                    _require(digest(canonical(orphan)) == manifest_hash, "orphan release conflict")
                else:
                    stage.rename(target)
                    self._sync_directory(target.parent)
                    self._sync_directory(self.root)

                db.execute("INSERT INTO releases VALUES (?,?,?,?)", (release_id, sequence, manifest_hash, canonical(manifest).decode()))
                if manifest["kind"] == "delta":
                    db.execute("INSERT INTO cases SELECT ?,case_id,body,asset_release FROM cases WHERE release_id=?",
                               (release_id, manifest["parent_release_id"]))
                for record in records:
                    case_id = record["case_id"]
                    db.execute("DELETE FROM cases WHERE release_id=? AND case_id=?", (release_id, case_id))
                    if record["op"] == "upsert":
                        db.execute("INSERT INTO cases VALUES (?,?,?,?)", (release_id, case_id, canonical(record["case"]).decode(), release_id))
                    db.execute("INSERT INTO history VALUES (?,?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision,record_hash=excluded.record_hash",
                               (case_id, record["revision"], digest(canonical(record))))
                # Smoke-check the full materialized view including inherited
                # assets before the single transactional pointer switch.
                for row in db.execute("SELECT body,asset_release FROM cases WHERE release_id=?", (release_id,)):
                    case = json.loads(row["body"])
                    for asset in case["images"]:
                        self._verified_asset(db, row["asset_release"], asset["path"])
                self._set(db, "active", release_id)
                self._set(db, "high_sequence", sequence)
                self._set(db, "high_release", release_id)
                return {"release_id": release_id, "status": "accepted", "active_release": release_id}
            finally:
                if stage.exists():
                    _require(stage.resolve().parent == self.root.resolve() and stage.name.startswith(".staging-"), "unsafe staging cleanup")
                    shutil.rmtree(stage)

    def apply_withdrawals(self, signed_document: dict,
                          trusted_keys: dict[str, Ed25519PublicKey]) -> dict:
        """Accept cumulative signed tombstones; rollback can never cancel them.

        A higher cumulative withdrawal sequence can safely skip intermediate
        documents, provided it contains every already received withdrawal.
        """
        _require(isinstance(signed_document, dict), "withdrawal must be an object")
        _require(len(canonical(signed_document)) <= MAX_RECORD_BYTES, "withdrawal inventory too large")
        verify_signature(signed_document, trusted_keys)
        _require(type(signed_document.get("schema_version")) is int and signed_document["schema_version"] == 1, "unsupported withdrawal schema")
        sequence = _integer(signed_document.get("sequence"))
        ids = signed_document.get("case_ids")
        _require(isinstance(ids, list) and bool(ids), "withdrawal cases required")
        ids = [_identifier(value) for value in ids]
        _require(len(ids) == len(set(ids)), "duplicate withdrawal ID")
        created_at = signed_document.get("created_at")
        _require(isinstance(created_at, str) and created_at.endswith("Z"), "withdrawal UTC created_at required")
        try:
            stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            _require(stamp.utcoffset() == timezone.utc.utcoffset(stamp), "withdrawal timestamp must be UTC")
        except ValueError as exc:
            raise KnowledgeError("invalid withdrawal timestamp") from exc
        document_hash = digest(canonical(signed_document))
        with _lock(self.root), self._db() as db:
            old = db.execute("SELECT document_hash FROM withdrawal_documents WHERE sequence=?", (sequence,)).fetchone()
            if old:
                _require(old[0] == document_hash, "withdrawal sequence conflict")
                return {"status": "already_applied", "sequence": sequence}
            high = int(self._get(db, "withdrawal_sequence", "0"))
            _require(sequence > high, "withdrawal sequence replay/out of order")
            existing = {row[0] for row in db.execute("SELECT case_id FROM withdrawals")}
            _require(existing <= set(ids), "cumulative withdrawals cannot be removed")
            db.executemany("INSERT OR IGNORE INTO withdrawals VALUES (?)", [(value,) for value in ids])
            db.execute("INSERT INTO withdrawal_documents VALUES (?,?)", (sequence, document_hash))
            self._set(db, "withdrawal_sequence", sequence)
            return {"status": "applied", "sequence": sequence, "withdrawn_cases": len(ids)}

    def rollback(self, release_id: str) -> dict:
        _identifier(release_id)
        with _lock(self.root), self._db() as db:
            _require(db.execute("SELECT 1 FROM releases WHERE id=?", (release_id,)).fetchone() is not None, "release not accepted locally")
            # Stored manifests were authenticated on receipt. Check every byte
            # of the local logical view before making an old version active.
            for row in db.execute("SELECT body,asset_release FROM cases WHERE release_id=?", (release_id,)):
                for asset in json.loads(row["body"])["images"]:
                    self._verified_asset(db, row["asset_release"], asset["path"])
            self._set(db, "active", release_id)
            return {"active_release": release_id, "high_sequence": int(self._get(db, "high_sequence", "0"))}

    def query_cases(self, keyword: str = "", tags: dict[str, list[str]] | None = None,
                    limit: int = 50) -> list[dict]:
        """Chinese substring + exact tag filtering, NOT semantic/vector search."""
        _require(isinstance(keyword, str) and len(keyword) <= 1000, "invalid keyword")
        _require(type(limit) is int and 1 <= limit <= 2000, "invalid query limit")
        tags = tags or {}
        _require(isinstance(tags, dict) and all(key in {"industry", "medium"} and isinstance(values, list) and all(isinstance(v, str) for v in values) for key, values in tags.items()), "invalid tag filter")
        with _lock(self.root), self._db() as db:
            rows = db.execute("SELECT c.body FROM cases c WHERE c.release_id=? AND NOT EXISTS (SELECT 1 FROM withdrawals w WHERE w.case_id=c.case_id) ORDER BY c.case_id", (self._get(db, "active"),))
            result = []
            for row in rows:
                case = json.loads(row[0])
                text = " ".join([case["title"], case["summary_zh"], case["brand_or_team"], *[claim["text"] for claim in case["claims"]], *case["tags"]["industry"], *case["tags"]["medium"]]).casefold()
                if keyword.casefold() not in text:
                    continue
                if any(not set(values) <= set(case["tags"][key]) for key, values in tags.items()):
                    continue
                result.append(case)
                if len(result) == limit:
                    break
            return result

    def _verified_asset(self, db, release_id: str, path: str) -> Path:
        row = db.execute("SELECT manifest FROM releases WHERE id=?", (release_id,)).fetchone()
        _require(row is not None, "asset release missing")
        manifest = json.loads(row[0])
        entry = next((entry for entry in manifest["files"] if entry["path"] == path), None)
        _require(entry is not None and path.startswith("images/"), "asset not in signed inventory")
        file = _file(self.root, f"releases/{release_id}/{path}")
        _require(file.is_file() and file.stat().st_size == entry["size"] and digest(file.read_bytes()) == entry["sha256"], "local asset corrupted or missing")
        return file

    def asset_path(self, case_id: str, image_id: str) -> Path:
        """Resolve an authorized-by-active-view local image, never an A URL.

        This is a trusted local harness API, not a download authorization token.
        A production BFF must check tenant rights and withdrawals at stream time.
        """
        _identifier(case_id)
        _identifier(image_id)
        with _lock(self.root), self._db() as db:
            row = db.execute("SELECT body,asset_release FROM cases WHERE release_id=? AND case_id=? AND NOT EXISTS (SELECT 1 FROM withdrawals WHERE case_id=?)", (self._get(db, "active"), case_id, case_id)).fetchone()
            _require(row is not None, "case unavailable or withdrawn")
            image = next((image for image in json.loads(row["body"])["images"] if image["image_id"] == image_id), None)
            _require(image is not None, "image does not belong to case")
            return self._verified_asset(db, row["asset_release"], image["path"])

    def status(self) -> dict:
        with _lock(self.root), self._db() as db:
            return {"active_release": self._get(db, "active") or None,
                    "high_sequence": int(self._get(db, "high_sequence", "0")),
                    "withdrawal_sequence": int(self._get(db, "withdrawal_sequence", "0")),
                    "withdrawn_cases": db.execute("SELECT count(*) FROM withdrawals").fetchone()[0],
                    "search_mode": "keyword_and_tags", "production_nova_connected": False}
