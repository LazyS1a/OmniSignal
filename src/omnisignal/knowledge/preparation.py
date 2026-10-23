"""Local operator-owned preparation workspace, outside the published file tree.

The store is a single editorial domain, not a consumer tenant database. Callers must
authenticate operators and derive actor from their identity, never request JSON.
Model work is serialized under a process lock; completed requests are cached.
Interrupted/failed requests require explicit retry because billing is uncertain.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
from typing import Protocol
from urllib.parse import urlsplit
import uuid

import httpx
from PIL import Image

from .release import (KnowledgeError, MAX_IMAGE_BYTES, MAX_RECORD_BYTES, _file, _identifier,
                      _image, _lock, _require, _text, canonical, digest, make_record,
                      publish_release, publish_withdrawals, validate_case)
from .taxonomy import ANALYSIS_SYSTEM_PROMPT, TAXONOMY, TAXONOMY_VERSION, TEMPLATE_VERSION

MAX_IMAGES = 6
MAX_INPUT_BYTES = 24 * 1024 * 1024
EDITABLE = {"title", "summary_zh", "brand_or_team", "tags", "claims", "permissions", "display_permission"}
PERMISSIONS = ("display", "download", "export", "generation", "analysis")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _permissions(value: dict) -> dict:
    permissions = value.get("permissions", {})
    _require(isinstance(permissions, dict) and not set(permissions) - set(PERMISSIONS), "invalid permissions")
    result = {key: permissions.get(key, "unknown") for key in PERMISSIONS}
    for status in result.values():
        _require(status in {"allowed", "denied", "unknown"}, "invalid permission state")
    legacy = value.get("display_permission", result["display"])
    _require(legacy in {"allowed", "denied", "unknown"}, "invalid display permission")
    if "display" in permissions:
        _require(legacy == result["display"], "conflicting display permission")
    result["display"] = legacy
    return result


def validate_editorial_case(case: dict) -> None:
    validate_case(case)
    for group, values in case["tags"].items():
        _require(group in TAXONOMY and isinstance(values, list), "unknown tag group")
        _require(all(isinstance(v, str) and v in TAXONOMY[group] for v in values), "unknown taxonomy tag")
    for claim in case["claims"]:
        if claim["kind"] in {"author_statement", "media_statement"}:
            _require(bool(claim.get("source_ids")), "source statement needs a source")
        if claim["kind"] == "visual_observation":
            _require(bool(claim.get("image_ids")), "visual observation needs an image")
    for entity in [case, *case["sources"], *case["images"]]:
        _require(_permissions(entity)["display"] == "allowed", "display permission required")


class AnalysisAdapter(Protocol):
    model_version: str
    cache_identity: str

    def analyze(self, case: dict, images: list[tuple[str, bytes]]) -> dict: ...


class QwenAnalysisAdapter:
    """OpenAI-compatible Qwen endpoint with A-only configuration and no retries."""
    def __init__(self, *, endpoint: str, api_key: str, model: str, timeout: float = 90,
                 transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(endpoint)
        _require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment, "A model endpoint must use HTTPS")
        _text(model, "A model")
        _require(bool(api_key.strip()), "A model credential missing")
        _require(1 <= timeout <= 180, "model timeout out of range")
        self.endpoint, self.api_key, self.model_version = endpoint.rstrip("/"), api_key, model
        self.cache_identity = digest(canonical({"endpoint": self.endpoint, "model": model}))
        self.timeout, self.transport = timeout, transport

    @classmethod
    def from_env(cls):
        return cls(endpoint=os.getenv("OMNISIGNAL_KNOWLEDGE_MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                   api_key=os.getenv("OMNISIGNAL_KNOWLEDGE_MODEL_API_KEY", ""),
                   model=os.getenv("OMNISIGNAL_KNOWLEDGE_MODEL", ""))

    def analyze(self, case: dict, images: list[tuple[str, bytes]]) -> dict:
        material = {key: case.get(key) for key in ("case_id", "title", "brand_or_team", "sources", "images")}
        # Local object paths and permission metadata have no model purpose.
        material["images"] = [{"image_id": x["image_id"], "source_id": x["source_id"]} for x in case["images"]]
        content = [{"type": "text", "text": canonical({"material": material, "taxonomy": TAXONOMY}).decode()}]
        for image_id, data in images:
            with Image.open(io.BytesIO(data)) as image:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
            content.extend([{"type": "text", "text": "image_id=" + image_id},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode()}}])
        body = {"model": self.model_version, "temperature": 0, "max_tokens": 3000,
                "response_format": {"type": "json_object"}, "messages": [
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT}, {"role": "user", "content": content}]}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", self.endpoint + "/chat/completions", headers={"Authorization": "Bearer " + self.api_key}, json=body) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        _require(len(data) <= MAX_RECORD_BYTES, "model response too large")
                    payload = json.loads(data)
            result = json.loads(payload["choices"][0]["message"]["content"])
            _require(isinstance(result, dict), "model JSON object required")
            return result
        except KnowledgeError:
            raise
        except Exception as exc:
            # Never persist provider bodies, request headers, or secrets in audit.
            raise KnowledgeError("A model request failed; operator retry required") from exc


class PreparationStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        _require(not self.root.is_symlink(), "preparation root symlink forbidden")
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets = _file(self.root, "assets")
        _file(self.root, "assets/images").mkdir(parents=True, exist_ok=True)
        self.database = _file(self.root, "preparation.sqlite3")
        if self.database.exists():
            with self._db() as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"cases", "audit", "tasks", "cache", "jobs", "history"}.issubset(tables):
                # Existing readers must remain available during bounded model work.
                return
        with _lock(self.root), self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, document TEXT NOT NULL, import_hash TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT,
                    actor TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(request_key TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                    result TEXT, error TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache(request_key TEXT PRIMARY KEY, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
                    retry_failed INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL, document TEXT NOT NULL, at TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        _file(self.root, "preparation.sqlite3")
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _audit(self, db, actor, action, case_id=None, details=None):
        _identifier(actor)
        db.execute("INSERT INTO audit(case_id, actor, action, at, details) VALUES(?,?,?,?,?)",
                   (case_id, actor, action, _now(), canonical(details or {}).decode()))

    def _write(self, db, case, status, import_hash):
        db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision, status=excluded.status, document=excluded.document, import_hash=excluded.import_hash, updated_at=excluded.updated_at",
                   (case["case_id"], case["revision"], status, canonical(case).decode(), import_hash, _now()))
        db.execute("INSERT INTO history(case_id,revision,status,document,at) VALUES(?,?,?,?,?)",
                   (case["case_id"], case["revision"], status, canonical(case).decode(), _now()))

    def get_case(self, case_id: str) -> dict:
        _identifier(case_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        _require(row is not None, "unknown case")
        return {"case": json.loads(row["document"]), "status": row["status"], "updated_at": row["updated_at"], "import_hash": row["import_hash"]}

    def list_cases(self) -> list[dict]:
        with self._db() as db:
            return [{"case_id": r["case_id"], "revision": r["revision"], "title": json.loads(r["document"])["title"], "status": r["status"], "updated_at": r["updated_at"]}
                    for r in db.execute("SELECT * FROM cases ORDER BY case_id")]

    def audit(self, case_id: str | None = None) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM audit WHERE (? IS NULL OR case_id=?) ORDER BY id DESC LIMIT 500", (case_id, case_id)).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def tasks(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT request_key,case_id,revision,status,attempts,error,updated_at FROM tasks ORDER BY updated_at DESC LIMIT 500")]

    def jobs(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 500")]

    def history(self, case_id: str) -> list[dict]:
        _identifier(case_id)
        with self._db() as db:
            rows = db.execute("SELECT * FROM history WHERE case_id=? ORDER BY id DESC LIMIT 500", (case_id,)).fetchall()
        return [{**dict(row), "document": json.loads(row["document"])} for row in rows]

    def enqueue_analysis(self, case_id: str, *, actor: str, expected_revision: int, retry_failed: bool = False) -> dict:
        _identifier(actor)
        with _lock(self.root), self._db() as db:
            current = self.get_case(case_id)
            _require(current["case"]["revision"] == expected_revision, "stale revision")
            _require(current["status"] not in {"approved", "withdrawn"}, "revise case before analysis")
            for entity in [current["case"], *current["case"]["sources"], *current["case"]["images"]]:
                _require(_permissions(entity)["analysis"] == "allowed", "external analysis permission required")
            previous = db.execute("SELECT * FROM jobs WHERE case_id=? AND revision=? ORDER BY created_at DESC LIMIT 1", (case_id, expected_revision)).fetchone()
            if previous and previous["status"] in {"pending", "running", "succeeded"}:
                return dict(previous)
            if previous:
                _require(retry_failed, "failed/interrupted job requires explicit retry")
            job_id, now = uuid.uuid4().hex, _now()
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (job_id, case_id, expected_revision, actor, "pending", int(retry_failed), None, now, now))
            self._audit(db, actor, "analysis_queued", case_id, {"job_id": job_id, "revision": expected_revision, "explicit_retry": retry_failed})
            return dict(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def run_pending(self, adapter: AnalysisAdapter) -> dict | None:
        with _lock(_file(self.root, "worker")):
            return self._run_pending(adapter)

    def recover_interrupted(self, *, actor: str) -> int:
        """Explicit maintenance; worker lock proves no live worker owns these jobs."""
        _identifier(actor)
        with _lock(_file(self.root, "worker")), _lock(self.root), self._db() as db:
            count = db.execute("UPDATE jobs SET status='interrupted',error='OperatorRecovery',updated_at=? WHERE status='running'", (_now(),)).rowcount
            self._audit(db, actor, "recover_interrupted", details={"jobs": count})
            return count

    def _run_pending(self, adapter: AnalysisAdapter) -> dict | None:
        # Worker claim is durable; never automatically reissue interrupted calls.
        with _lock(self.root), self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            job = dict(row)
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE job_id=?", (_now(), job["job_id"]))
        try:
            self.analyze(job["case_id"], adapter, actor=job["actor"], retry_failed=bool(job["retry_failed"]), expected_revision=job["revision"])
            status, error = "succeeded", None
        except Exception as exc:
            status, error = "failed", type(exc).__name__
        with self._db() as db:
            db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE job_id=?", (status, error, _now(), job["job_id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone())

    def import_case(self, case: dict, asset_root: Path | str, *, actor: str) -> dict:
        _identifier(actor)
        _require(isinstance(case, dict) and len(canonical(case)) <= MAX_RECORD_BYTES, "invalid/oversized import")
        case = json.loads(canonical(case))
        _identifier(case.get("case_id"))
        for field in ("title", "brand_or_team"):
            _text(case.get(field), field)
        _require(type(case.get("revision", 1)) is int and case.get("revision", 1) >= 1, "invalid revision")
        case.setdefault("revision", 1)
        _require(isinstance(case.get("sources"), list) and 0 < len(case["sources"]) <= 30, "sources required")
        _require(isinstance(case.get("images"), list) and 0 < len(case["images"]) <= MAX_IMAGES, "1 to 6 images required")
        source_ids = set()
        for source in case["sources"]:
            _require(isinstance(source, dict), "source must be an object")
            _identifier(source.get("source_id"))
            _require(source["source_id"] not in source_ids, "duplicate source ID")
            source_ids.add(source["source_id"])
            _require(isinstance(source.get("url"), str) and len(source["url"]) <= 8192, "source URL required")
            try:
                parsed = urlsplit(source["url"])
            except ValueError as exc:
                raise KnowledgeError("invalid source URL") from exc
            _require(parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username and not parsed.password, "invalid source URL")
            for field in ("author", "published_at", "collected_at", "evidence_locator"):
                source.setdefault(field, None)
            source["permissions"] = _permissions(source)
            source["display_permission"] = source["permissions"]["display"]
        image_ids, pending, total = set(), [], 0
        for image in case["images"]:
            _require(isinstance(image, dict), "image must be an object")
            _identifier(image.get("image_id"))
            _require(image["image_id"] not in image_ids, "duplicate image ID")
            image_ids.add(image["image_id"])
            _require(image.get("source_id") in source_ids, "unknown image source")
            path = _file(Path(asset_root), image.get("path"))
            _require(path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES, "missing or oversized image")
            data = path.read_bytes()
            total += len(data)
            _require(total <= MAX_INPUT_BYTES, "import image total too large")
            _image(data)
            sha = digest(data)
            with Image.open(io.BytesIO(data)) as decoded:
                ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[decoded.format]
                image.update(width=decoded.width, height=decoded.height, format=decoded.format)
            image.update(path=f"images/{sha}.{ext}", sha256=sha, size=len(data))
            image["permissions"] = _permissions(image)
            image["display_permission"] = image["permissions"]["display"]
            image.setdefault("role", "reference")
            pending.append((image["path"], data))
        case["permissions"] = _permissions(case)
        case["display_permission"] = case["permissions"]["display"]
        case.update(review_status="pending", taxonomy_version=TAXONOMY_VERSION)
        # Imported approval/model attribution is not trusted.
        case.pop("review", None)
        case["analysis_version"], case["model_version"] = "manual-1", "manual"
        fingerprint = digest(canonical(case))
        with _lock(self.root), self._db() as db:
            old = db.execute("SELECT * FROM cases WHERE case_id=?", (case["case_id"],)).fetchone()
            if old and old["import_hash"] == fingerprint:
                return self.get_case(case["case_id"])
            if old:
                _require(case["revision"] > old["revision"], "changed import requires higher revision")
                _require(old["status"] != "withdrawn", "withdrawn case cannot be imported again")
            for name, data in pending:
                destination = _file(self.assets, name)
                if destination.exists():
                    _require(digest(destination.read_bytes()) == digest(data), "stored image hash mismatch")
                else:
                    with destination.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
            self._write(db, case, "pending", fingerprint)
            self._audit(db, actor, "import", case["case_id"], {"revision": case["revision"], "input_hash": fingerprint})
        return self.get_case(case["case_id"])

    def revise(self, case_id: str, patch: dict, *, actor: str, expected_revision: int) -> dict:
        _identifier(actor)
        _require(isinstance(patch, dict) and bool(patch) and not set(patch) - EDITABLE, "invalid revision fields")
        with _lock(self.root), self._db() as db:
            current = self.get_case(case_id)
            _require(current["status"] != "withdrawn", "withdrawn case cannot be revised")
            case = current["case"]
            _require(case["revision"] == expected_revision, "stale revision")
            before = digest(canonical(case))
            case.update(json.loads(canonical(patch)))
            case["permissions"] = _permissions(case)
            case["display_permission"] = case["permissions"]["display"]
            case.update(revision=case["revision"] + 1, review_status="pending")
            case.pop("review", None)
            _require(len(canonical(case)) <= MAX_RECORD_BYTES, "case too large")
            self._write(db, case, "pending", current["import_hash"])
            self._audit(db, actor, "revise", case_id, {"before_hash": before, "after_hash": digest(canonical(case)), "fields": sorted(patch), "revision": case["revision"]})
        return self.get_case(case_id)

    def analyze(self, case_id: str, adapter: AnalysisAdapter, *, actor: str, retry_failed: bool = False, expected_revision: int | None = None) -> dict:
        _identifier(actor)
        with _lock(self.root):
            current = self.get_case(case_id)
            case = current["case"]
            _require(expected_revision is None or case["revision"] == expected_revision, "stale analysis job")
            _require(current["status"] not in {"approved", "withdrawn"}, "revise case before analysis")
            for entity in [case, *case["sources"], *case["images"]]:
                _require(_permissions(entity)["analysis"] == "allowed", "external analysis permission required")
            images = []
            for item in case["images"]:
                path = _file(self.assets, item["path"])
                _require(path.stat().st_size == item["size"], "stored image size mismatch")
                data = path.read_bytes()
                _require(digest(data) == item["sha256"], "stored image hash mismatch")
                images.append((item["image_id"], data))
            # Exclude prior analysis fields so repeated invocation hits the same cache.
            material = {key: case.get(key) for key in ("case_id", "title", "brand_or_team", "sources", "images")}
            request_key = digest(canonical({"material": material, "model": adapter.model_version,
                                            "provider": adapter.cache_identity, "template": TEMPLATE_VERSION}))
            with self._db() as db:
                cached = db.execute("SELECT result FROM cache WHERE request_key=?", (request_key,)).fetchone()
                previous = db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
                if previous and not cached:
                    _require(retry_failed and previous["attempts"] < 3, "interrupted/failed analysis requires explicit retry; maximum 3 attempts")
                if not cached:
                    db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET status='running', attempts=tasks.attempts+1, error=NULL, updated_at=excluded.updated_at",
                               (request_key, case_id, case["revision"], "running", 1, None, None, _now()))
                    self._audit(db, actor, "analysis_started", case_id, {"request_key": request_key, "model_version": adapter.model_version})
            try:
                result = json.loads(cached["result"]) if cached else adapter.analyze(case, images)
                _require(isinstance(result, dict) and set(result) == {"summary_zh", "tags", "claims"}, "unexpected analysis fields")
                candidate = {**case, **result, "analysis_version": TEMPLATE_VERSION, "model_version": adapter.model_version, "review_status": "approved"}
                # Analyzable content may still await display permission review.
                validation_copy = json.loads(canonical(candidate))
                for entity in [validation_copy, *validation_copy["sources"], *validation_copy["images"]]:
                    entity["display_permission"] = "allowed"
                    entity["permissions"]["display"] = "allowed"
                validate_editorial_case(validation_copy)
                candidate["review_status"] = "pending"
                candidate.pop("review", None)
                if canonical(candidate) != canonical(case):
                    candidate["revision"] = case["revision"] + 1
                _require(len(canonical(candidate)) <= MAX_RECORD_BYTES, "analysis too large")
                with self._db() as db:
                    db.execute("INSERT OR IGNORE INTO cache VALUES(?,?)", (request_key, canonical(result).decode()))
                    db.execute("UPDATE tasks SET status='succeeded',result=?,error=NULL,updated_at=? WHERE request_key=?", (canonical(result).decode(), _now(), request_key))
                    self._write(db, candidate, "pending", current["import_hash"])
                    self._audit(db, actor, "analysis_completed", case_id, {"request_key": request_key, "cache_hit": bool(cached)})
            except Exception as exc:
                with self._db() as db:
                    db.execute("UPDATE tasks SET status='failed',error=?,updated_at=? WHERE request_key=?", (type(exc).__name__, _now(), request_key))
                    self._audit(db, actor, "analysis_failed", case_id, {"request_key": request_key, "error_type": type(exc).__name__})
                raise KnowledgeError("analysis failed validation or provider request; see task status") from exc
        return self.get_case(case_id)

    def review(self, case_id: str, decision: str, *, actor: str, expected_revision: int, note: str = "") -> dict:
        _identifier(actor)
        _require(decision in {"approve", "return"}, "invalid review decision")
        _require(isinstance(note, str) and len(note) <= 4000, "invalid review note")
        _require(decision != "return" or bool(note.strip()), "return reason required")
        with _lock(self.root), self._db() as db:
            current = self.get_case(case_id)
            case = current["case"]
            _require(case["revision"] == expected_revision, "stale revision")
            _require(current["status"] not in {"approved", "withdrawn"}, "review already finalized; revise first")
            case["review_status"] = "approved" if decision == "approve" else "returned"
            if decision == "approve":
                validate_editorial_case(case)
            case["review"] = {"actor": actor, "at": _now(), "decision": decision, "note": note}
            self._write(db, case, case["review_status"], current["import_hash"])
            self._audit(db, actor, "review_" + decision, case_id, {"revision": case["revision"], "note": note, "content_hash": digest(canonical(case))})
        return self.get_case(case_id)

    def publish(self, *, publication_root: Path | str, release_id: str, sequence: int,
                private_key, key_id: str, actor: str, kind: str = "full",
                parent_release_id: str | None = None, case_ids: list[str] | None = None) -> dict:
        _identifier(actor)
        publication_root = Path(publication_root)
        _require(not self.root.resolve().is_relative_to(publication_root.resolve()) and not publication_root.resolve().is_relative_to(self.root.resolve()), "preparation and publication must use separate roots")
        with _lock(self.root):
            all_cases = self.list_cases()
            if kind == "full":
                _require(case_ids is None, "full publication includes all non-withdrawn cases")
                selected = [item["case_id"] for item in all_cases if item["status"] != "withdrawn"]
            else:
                _require(kind == "delta" and isinstance(case_ids, list) and bool(case_ids) and len(case_ids) == len(set(case_ids)), "delta requires unique case IDs")
                selected = case_ids
            _require(bool(selected), "no reviewed content to publish")
            records = []
            for case_id in selected:
                current = self.get_case(case_id)
                _require(current["status"] == "approved" and bool(current["case"].get("review")), "all selected content must have human approval")
                validate_editorial_case(current["case"])
                records.append(make_record(current["case"]))
            with self._db() as db:
                self._audit(db, actor, "publication_started", details={"release_id": release_id, "sequence": sequence, "cases": selected})
            try:
                result = publish_release(root=publication_root, release_id=release_id, sequence=sequence, records=records,
                                         asset_root=self.assets, private_key=private_key, key_id=key_id, kind=kind,
                                         parent_release_id=parent_release_id, taxonomy_version=TAXONOMY_VERSION)
            except Exception as exc:
                with self._db() as db:
                    self._audit(db, actor, "publication_failed", details={"release_id": release_id, "error_type": type(exc).__name__})
                raise
            with self._db() as db:
                self._audit(db, actor, "publication_completed", details={"release_id": release_id, "sequence": sequence, "key_id": key_id})
            return result

    def withdraw(self, *, publication_root: Path | str, sequence: int, case_ids: list[str], private_key,
                 key_id: str, actor: str, reason: str) -> dict:
        _identifier(actor)
        _text(reason, "withdrawal reason")
        _require(bool(case_ids) and len(case_ids) == len(set(case_ids)), "withdrawal IDs required")
        with _lock(self.root):
            cases = [self.get_case(case_id) for case_id in case_ids]
            with self._db() as db:
                self._audit(db, actor, "withdrawal_started", details={"sequence": sequence, "cases": case_ids, "reason": reason})
            result = publish_withdrawals(root=publication_root, sequence=sequence, case_ids=case_ids, private_key=private_key, key_id=key_id)
            with self._db() as db:
                for current in cases:
                    case = current["case"]
                    case["review_status"] = "withdrawn"
                    self._write(db, case, "withdrawn", current["import_hash"])
                    self._audit(db, actor, "withdrawn", case["case_id"], {"sequence": sequence, "reason": reason})
            return result
