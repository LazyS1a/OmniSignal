"""Opt-in local AI editorial worker. Scheduling model calls belongs to DailyPilot.

This service never infers use rights or labels model work as human review.
An explicit operator policy can authorize scoped internal delivery separately
from original permissions. It accepts a completed, current Qwen job and signs eligible
local releases; a signed release is not a downstream delivery receipt.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading

from omnisignal.knowledge.release import KnowledgeError, _lock, read_latest
from .publishing import publish_store_assets, publication_snapshot_ids, select_capacity_batch, CAPACITY_FAILURES
from .review import apply_automated_review, fresh_review, validate_automated_quality
from .store import Conflict, asset_row_consistent, encode, require, stamp
from .taxonomy import fresh_taxonomy
from .publication_use import ensure_table, permits_internal_delivery, validate_policy, library_recipient, target_delivered
from .library_publication import target_root
from .receiver_budget import select_publication_batch, ReceiptUnavailable
from .source_adapters import SOURCE_CONFIGS as EXTRA_SOURCES
from omnisignal.knowledge.scope import recipient_key

ACTOR = "system:auto-editorial"
CONFIG_ENV = "OMNISIGNAL_WORKBENCH_AUTO_EDITORIAL_CONFIG"
SOURCE_IDS = {"potw", "adsofbrands", "bpando", "itsnicethat"} | set(EXTRA_SOURCES)
CONFIG_KEYS = {"schema_version", "enabled", "source_ids", "analysis_authorized", "auto_publish", "interval_seconds", "max_per_tick"}
LOCAL_REVIEW_RETRY_DELAYS = (60, 300)
LOCAL_REVIEW_MAX_IO_ATTEMPTS = len(LOCAL_REVIEW_RETRY_DELAYS) + 1


def validate_config(value):
    require(isinstance(value, dict) and set(value) in (CONFIG_KEYS, CONFIG_KEYS | {"publication_authorized"}) and value["schema_version"] == 1,
            "invalid_auto_editorial_config")
    require(type(value["enabled"]) is bool and type(value["auto_publish"]) is bool, "invalid_auto_editorial_config")
    sources = value["source_ids"]
    require(isinstance(sources, list) and bool(sources) and all(isinstance(source, str) and source in SOURCE_IDS for source in sources)
            and len(sources) == len(set(sources)), "invalid_source_scope")
    require(type(value["interval_seconds"]) is int and 5 <= value["interval_seconds"] <= 3600
            and type(value["max_per_tick"]) is int and 1 <= value["max_per_tick"] <= 500, "invalid_auto_editorial_config")
    grant = value["analysis_authorized"]
    if grant is not None:
        require(isinstance(grant, dict) and set(grant) == {"policy_id", "authorized_at", "purpose"}
                and isinstance(grant["policy_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", grant["policy_id"])
                and grant["purpose"] == "image_analysis_and_ai_review", "invalid_analysis_authorization")
        try:
            parsed = datetime.fromisoformat(grant["authorized_at"])
            require(parsed.tzinfo is not None, "invalid_analysis_authorization")
        except (TypeError, ValueError):
            raise Conflict("invalid_analysis_authorization") from None
    validate_policy(value.get("publication_authorized"), source_ids=sources)
    return deepcopy(value)


class AutoEditorial:
    def __init__(self, store, analysis=None, *, config=None, policy_path=None, publication_root=None):
        self.store, self.analysis = store, analysis
        self._config = deepcopy(config)
        self.policy_path = Path(policy_path or os.environ[CONFIG_ENV]) if policy_path or os.getenv(CONFIG_ENV) else None
        self.publication_root = publication_root
        self._thread = None
        self._stop = threading.Event()
        with store.db() as db:
            ensure_table(db)
            db.execute("""CREATE TABLE IF NOT EXISTS auto_editorial_outcomes(
                asset_id TEXT NOT NULL, revision INTEGER NOT NULL, job_id TEXT NOT NULL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(asset_id,revision,job_id))""")
            # Keep operational retries separate from permanent quality outcomes.
            # A separate additive table leaves the legacy outcome schema intact.
            db.execute("""CREATE TABLE IF NOT EXISTS auto_editorial_local_retries(
                asset_id TEXT NOT NULL, revision INTEGER NOT NULL, job_id TEXT NOT NULL,
                attempts INTEGER NOT NULL, next_attempt_at REAL, reason TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(asset_id,revision,job_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS auto_editorial_quality_holds(
                asset_id TEXT NOT NULL, revision INTEGER NOT NULL, job_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, PRIMARY KEY(asset_id,revision,job_id))""")

    def _quality_fingerprint(self, db, job):
        from .design_skills import details_digest, get_analysis_details, versions
        return details_digest({"details": get_analysis_details(db, job["id"]), "validator": versions()})

    def _publication_batch_key(self, config):
        recipient = library_recipient(config.get("publication_authorized"))
        return "auto_editorial_publication_batch" + (":" + recipient_key(recipient) if recipient is not None else "")

    def configuration(self):
        if self._config is None and self.policy_path is None:
            return None
        value = self._config
        if self.policy_path is not None:
            require(self.policy_path.is_file() and not self.policy_path.is_symlink()
                    and self.policy_path.stat().st_size <= 16000, "invalid_auto_editorial_config")
            value = json.loads(self.policy_path.read_text(encoding="utf-8"))
        return validate_config(value)

    def _record(self, asset, outcome, reason):
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO auto_editorial_outcomes VALUES(?,?,?,?,?,?)",
                       (asset["id"], asset["revision"], asset.get("analysis_job_id") or "none", outcome, reason, stamp()))

    def _record_local_io_failure(self, asset, job_id):
        """Bound local rechecks of the same paid result; never call the model."""
        identity = (asset["id"], asset["revision"], job_id)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT attempts FROM auto_editorial_local_retries WHERE asset_id=? AND revision=? AND job_id=?", identity).fetchone()
            attempts = min(LOCAL_REVIEW_MAX_IO_ATTEMPTS, (prior[0] if prior else 0) + 1)
            next_at = (self.store.clock() + LOCAL_REVIEW_RETRY_DELAYS[attempts - 1]
                       if attempts < LOCAL_REVIEW_MAX_IO_ATTEMPTS else None)
            reason = "local_review_io_retry_wait" if next_at is not None else "local_review_io_retry_exhausted"
            db.execute("INSERT OR REPLACE INTO auto_editorial_local_retries VALUES(?,?,?,?,?,?,?)",
                       (*identity, attempts, next_at, reason, stamp()))
            self.store.audit(db, asset["id"], ACTOR, "automated_review_io_retry",
                             {"revision": asset["revision"], "source_job_id": job_id, "attempt": attempts,
                              "next_attempt_at": next_at, "reason": reason, "provider_calls": 0})
        return reason

    def _clear_local_retry(self, identity):
        with self.store.db() as db:
            db.execute("DELETE FROM auto_editorial_local_retries WHERE asset_id=? AND revision=? AND job_id=?", identity)

    def _authorize(self, config):
        if config["analysis_authorized"] is None:
            return 0
        granted = 0
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM assets WHERE status='pending' ORDER BY created_at,id").fetchall()
            for row in rows:
                if not asset_row_consistent(row):
                    continue
                asset = json.loads(row["document"])
                if (asset.get("source_id") not in config["source_ids"] or asset.get("permissions", {}).get("analysis") != "unknown"
                        or asset.get("reviewed_analysis") or asset.get("reviewed_taxonomy")):
                    continue
                revision = asset["revision"]
                asset["permissions"] = {**asset["permissions"], "analysis": "allowed"}
                asset["analysis_authorization"] = {**config["analysis_authorized"], "source_ids": config["source_ids"], "actor": ACTOR}
                note = "本计划已允许 AI 分析与自动复核；展示和导出权限保持原记录。"
                asset["permission_note"] = ((asset.get("permission_note") or "")[:3900] + "\n" + note).strip()
                asset["revision"] += 1
                asset["analysis_status"] = "not_analyzed"
                require(db.execute("UPDATE assets SET revision=?,document=? WHERE id=? AND revision=?",
                                   (asset["revision"], encode(asset), asset["id"], revision)).rowcount == 1, "revision_changed")
                self.store.audit(db, asset["id"], ACTOR, "analysis_permission_granted", asset["analysis_authorization"])
                granted += 1
                if granted >= config["max_per_tick"]:
                    break
        return granted

    def tick(self):
        try:
            config = self.configuration()
        except (Conflict, ValueError, OSError, TypeError):
            return {"status": "invalid_config", "human_reviewed": None, "receipt_status": "unconfirmed"}
        if config is None or not config["enabled"]:
            return {"status": "disabled", "human_reviewed": None, "receipt_status": "unconfirmed"}
        try:
            with _lock(self.store.root / ".auto-editorial"):
                return self._tick_locked(config)
        except KnowledgeError:
            return {"status": "busy", "human_reviewed": None, "receipt_status": "unconfirmed"}

    def _discard_unsigned_stale_batch(self, db, batch, config):
        """Human edits may invalidate an intent before signing. Cancel only
        when its exact frozen identity proves no immutable release exists.
        Already signed, uncertain, or legacy identity remains for reconciliation.
        Caller holds BEGIN IMMEDIATE; use the normal DB -> publisher lock order.
        """
        if not isinstance(batch, list) or not batch or any(not isinstance(item, dict)
                or set(item) != {"id", "revision", "sha256"} or not isinstance(item["id"], str)
                or type(item["revision"]) is not int or not isinstance(item["sha256"], str)
                or not re.fullmatch("[a-f0-9]{64}", item["sha256"]) for item in batch):
            return False
        stale = False
        for item in batch:
            row = db.execute("SELECT * FROM assets WHERE id=?", (item["id"],)).fetchone()
            asset = json.loads(row["document"]) if row else {}
            stale = stale or (not asset_row_consistent(row)
                or asset.get("revision") != item["revision"] or asset.get("sha256") != item["sha256"]
                or asset.get("status") != "accepted" or asset.get("publication_status") == "withdrawn"
                or asset.get("source_id") not in config["source_ids"]
                or not permits_internal_delivery(asset, config.get("publication_authorized")))
            if asset.get("reviewed_analysis", {}).get("provenance") == "model_automated_review":
                try:
                    validate_automated_quality(db, asset)
                except (Conflict, ValueError, KeyError, TypeError):
                    stale = True
        if not stale:
            return False
        recipient = library_recipient(config.get("publication_authorized"))
        root = target_root(self.store, config.get("publication_authorized"), self.publication_root)
        ids = publication_snapshot_ids(batch, recipient)
        with _lock(root / ".workbench"), _lock(root):
            if any((root / "releases" / value).exists() or (root / "releases" / value).is_symlink()
                   or db.execute("SELECT 1 FROM releases WHERE version=?", (value,)).fetchone() for value in ids):
                return False
            deleted = db.execute("DELETE FROM meta WHERE key=? AND value=?", (self._publication_batch_key(config), encode(batch)))
            if deleted.rowcount:
                self.store.audit(db, ids[0], ACTOR, "unsigned_publication_cancelled",
                                 {"assets": [item["id"] for item in batch], "reason": "source_snapshot_changed"})
            return bool(deleted.rowcount)

    def _tick_locked(self, config):
        recipient = library_recipient(config.get("publication_authorized"))
        batch_key = self._publication_batch_key(config)
        result = {"status": "ready", "at": stamp(), "actor": ACTOR, "authorized": self._authorize(config),
                  "reviewed": 0, "published": 0, "blocked": 0, "human_reviewed": None,
                  "receipt_status": "unconfirmed", "releases": []}
        reasons = Counter()
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM assets WHERE status IN ('pending','accepted') ORDER BY created_at,id").fetchall()
        attempted, publishable = 0, []
        for row in rows:
            asset = json.loads(row["document"])
            if asset.get("source_id") not in config["source_ids"]:
                continue
            if not asset_row_consistent(row):
                reasons["storage_inconsistent"] += 1
                continue
            if asset["status"] == "pending":
                job_id = asset.get("analysis_job_id")
                if not job_id:
                    reason = "analysis_permission_denied" if asset.get("permissions", {}).get("analysis") == "denied" else "waiting_for_scheduled_analysis"
                    reasons[reason] += 1
                    continue
                with self.store.db() as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_jobs'").fetchone()
                    job = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone() if exists else None
                    prior = db.execute("SELECT outcome,reason FROM auto_editorial_outcomes WHERE asset_id=? AND revision=? AND job_id=?", (asset["id"], asset["revision"], job_id)).fetchone()
                    local_retry = db.execute("SELECT attempts,next_attempt_at FROM auto_editorial_local_retries WHERE asset_id=? AND revision=? AND job_id=?", (asset["id"], asset["revision"], job_id)).fetchone()
                    from .design_skills import details_required
                    specialized = job is not None and details_required(dict(job))
                    fingerprint = self._quality_fingerprint(db, job) if specialized else None
                    hold = db.execute("SELECT fingerprint FROM auto_editorial_quality_holds WHERE asset_id=? AND revision=? AND job_id=?", (asset["id"], asset["revision"], job_id)).fetchone()
                if not job or job["status"] != "completed":
                    reasons["failed_job_needs_explicit_retry" if job and job["status"] in {"failed", "interrupted"} else "waiting_or_stale_job"] += 1
                    continue
                if prior and prior["outcome"] == "manual_required" and not specialized:
                    reasons["manual_review_required"] += 1
                    continue
                if prior and prior["outcome"] == "exception_held" and hold and hold[0] == fingerprint:
                    reasons[prior["reason"]] += 1
                    continue
                if local_retry:
                    if local_retry["attempts"] >= LOCAL_REVIEW_MAX_IO_ATTEMPTS:
                        reasons["local_review_io_retry_exhausted"] += 1
                        continue
                    if local_retry["next_attempt_at"] > self.store.clock():
                        reasons["local_review_io_retry_wait"] += 1
                        continue
                if attempted >= config["max_per_tick"]:
                    break
                attempted += 1
                review_identity = (asset["id"], asset["revision"], job_id)
                try:
                    asset = apply_automated_review(self.store, asset["id"], asset["revision"], job_id)
                    self._clear_local_retry(review_identity)
                    result["reviewed"] += 1
                except OSError:
                    reasons[self._record_local_io_failure(asset, job_id)] += 1
                    continue
                except (Conflict, KeyError, TypeError):
                    reason = "content_quality_attention" if specialized else "manual_review_required"
                    self._record(asset, "exception_held" if specialized else "manual_required",
                                 reason if specialized else "freshness_or_quality_gate")
                    if specialized:
                        with self.store.db() as db:
                            db.execute("INSERT OR REPLACE INTO auto_editorial_quality_holds VALUES(?,?,?,?)", (*review_identity, fingerprint))
                    self._clear_local_retry(review_identity)
                    reasons[reason] += 1
                    continue
            if asset.get("reviewed_analysis", {}).get("provenance") != "model_automated_review":
                continue
            if recipient is None:
                if asset.get("publication_status") == "published":
                    continue
            else:
                with self.store.db() as db:
                    if target_delivered(db, asset, recipient):
                        continue
            try:
                fresh_review(asset)
                fresh_taxonomy(asset)
                with self.store.db() as db:
                    validate_automated_quality(db, asset)
            except (Conflict, KeyError, TypeError):
                reasons["review_revision_or_quality_changed"] += 1
                continue
            if not config["auto_publish"]:
                reasons["automatic_publication_disabled"] += 1
                continue
            if not permits_internal_delivery(asset, config.get("publication_authorized")):
                reasons["display_export_permission_required"] += 1
                continue
            if len(publishable) < min(25, config["max_per_tick"]):
                publishable.append({"id": asset["id"], "revision": asset["revision"], "sha256": asset["sha256"]})
        if config["auto_publish"]:
            # Ask the receiver before freezing membership. Signed outstanding
            # packages reserve allowance across ticks/restarts and date changes.
            # A frozen recovery batch is never truncated to fit today's quota.
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                frozen = db.execute("SELECT value FROM meta WHERE key=?", (batch_key,)).fetchone()
                if frozen and self._discard_unsigned_stale_batch(db, json.loads(frozen[0]), config):
                    frozen = None
            frozen_batch = json.loads(frozen[0]) if frozen else None
            delivery_allowed = True
            try:
                selected, delivery = select_publication_batch(self.store, frozen_batch or publishable,
                    config.get("publication_authorized"), publication_root=self.publication_root)
                result["delivery_budget"] = delivery
                if frozen_batch is not None:
                    delivery_allowed = selected == frozen_batch
                    if not delivery_allowed:
                        reasons["receiver_daily_limit"] += len(frozen_batch)
                else:
                    reasons["receiver_daily_limit"] += len(publishable) - len(selected)
                    publishable = selected
            except (ReceiptUnavailable, OSError, ValueError, KeyError, TypeError):
                delivery_allowed = False
                result["delivery_budget"] = {"status": "unavailable", "receipt_status": "unconfirmed"}
                reasons["receiver_receipt_required"] += 1
            # Only unsigned/unfrozen candidates may be split. Recovery retains
            # its exact membership, even after a capacity setting is lowered.
            if delivery_allowed and frozen_batch is None:
                try:
                    publishable, capacity = select_capacity_batch(self.store, publishable)
                    result["publication_capacity"] = capacity
                    reasons["publication_capacity_deferred"] += capacity["deferred"]
                except (Conflict, KnowledgeError, OSError, ValueError, KeyError, TypeError):
                    delivery_allowed = False
                    reasons["publication_capacity_unavailable"] += 1
                    result["publication_capacity"] = {"status": "unavailable"}
            # Commit batch membership before any signature. A later tick first
            # reconciles that same batch even if more images arrived meanwhile.
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                pending = db.execute("SELECT value FROM meta WHERE key=?", (batch_key,)).fetchone()
                batch = json.loads(pending[0]) if pending else publishable
                if pending and self._discard_unsigned_stale_batch(db, batch, config):
                    # A replacement must pass its own downstream preflight on
                    # the next tick; the discarded batch's quota is not a grant.
                    pending, batch = None, []
                if batch and not pending and delivery_allowed:
                    db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (batch_key, encode(batch)))
            if batch and delivery_allowed:
                try:
                    require(all((self.store.asset(item["id"]) or {}).get("source_id") in config["source_ids"] for item in batch),
                            "publication_scope_changed")
                    receipt = publish_store_assets(self.store, [item["id"] for item in batch], ACTOR,
                        expected_revisions={item["id"]: item["revision"] for item in batch}, automated_only=True,
                        publication_root=self.publication_root,
                        publication_policy=config.get("publication_authorized"), policy_loader=self.configuration)
                    with self.store.db() as db:
                        db.execute("DELETE FROM meta WHERE key=? AND value=?", (batch_key, encode(batch)))
                    result["releases"].append(receipt)
                    result["published"] = 0 if receipt.get("replayed") else len(batch)
                except KnowledgeError as error:
                    if str(error) in CAPACITY_FAILURES:
                        reasons["publication_capacity_exceeded"] += len(batch)
                        result["publication_capacity"] = {"status": "blocked", "reason": "final_release_capacity_exceeded"}
                    else:
                        reasons["publication_blocked"] += len(batch)
                except (Conflict, OSError, ValueError, TypeError):
                    reasons["publication_blocked"] += len(batch)
        result["blocked_reasons"] = dict(reasons)
        result["blocked"] = sum(reasons.values())
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('auto_editorial_status',?)", (encode(result),))
        return result

    def status(self):
        try:
            config = self.configuration()
            state = "enabled" if config and config["enabled"] else "disabled"
        except (Conflict, ValueError, OSError, TypeError):
            config, state = None, "invalid_config"
        with self.store.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='auto_editorial_status'").fetchone()
            day = datetime.fromtimestamp(self.store.clock(), timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat()
            audits = db.execute("SELECT entity,action,details FROM audit WHERE actor=? AND date(at,'+8 hours')=?",
                                (ACTOR, day)).fetchall()
            counts = {key: set() for key in ("authorized", "reviewed", "published")}
            for audit in audits:
                if audit["action"] == "analysis_permission_granted":
                    counts["authorized"].add(audit["entity"])
                elif audit["action"] == "automated_review":
                    counts["reviewed"].add(audit["entity"])
                elif audit["action"] == "publish":
                    counts["published"].update(json.loads(audit["details"]).get("assets", []))
            awaiting, permission_blocked = 0, 0
            has_jobs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_jobs'").fetchone()
            for item in db.execute("SELECT document FROM assets WHERE status IN ('pending','accepted')").fetchall():
                asset = json.loads(item[0])
                if not config or asset.get("source_id") not in config["source_ids"]:
                    continue
                if asset["status"] == "pending" and has_jobs:
                    completed = db.execute("SELECT 1 FROM analysis_jobs WHERE id=? AND status='completed'",
                                           (asset.get("analysis_job_id"),)).fetchone()
                    awaiting += int(bool(completed))
                if asset["status"] == "accepted" and asset.get("reviewed_analysis", {}).get("provenance") == "model_automated_review":
                    permission_blocked += int(not permits_internal_delivery(asset, config.get("publication_authorized") if config else None))
            today = {"date": day, "timezone": "Asia/Shanghai", **{name: len(values) for name, values in counts.items()},
                     "awaiting_review": awaiting, "permission_blocked": permission_blocked, "human_reviewed": None}
            recipient = library_recipient(config.get("publication_authorized")) if config else None
            delivered = db.execute("SELECT count(*) FROM publication_target_deliveries WHERE recipient_key=?",
                                   (recipient_key(recipient),)).fetchone()[0] if recipient is not None else None
            target_latest = read_latest(target_root(self.store, config.get("publication_authorized"), self.publication_root)) if recipient is not None else None
        return {"status": state, "actor": ACTOR, "source_ids": config["source_ids"] if config else [],
                "publication_recipient": recipient, "target_delivered_revisions": delivered, "target_latest_release": target_latest,
                "auto_publish": bool(config and config["auto_publish"]),
                "publication_authorized": bool(config and config.get("publication_authorized")),
                "analysis_authorized": bool(config and config["analysis_authorized"]),
                "human_reviewed": None, "receipt_status": "unconfirmed",
                "last_tick": json.loads(row[0]) if row else None,
                "today": today,
                "running": self._thread is not None and self._thread.is_alive()}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        def work():
            while not self._stop.is_set():
                interval = 30
                try:
                    config = self.configuration()
                    interval = config["interval_seconds"] if config else interval
                    self.tick()
                except Exception:
                    # Do not leak provider bodies/materials or convert an error
                    # into a retry of a billable model request.
                    pass
                self._stop.wait(interval)
        self._thread = threading.Thread(target=work, name="omni-auto-editorial", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
