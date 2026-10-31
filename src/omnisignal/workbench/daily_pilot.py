"""Opt-in bounded daily scheduling; no collection, permission, review or publication writes."""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid

from omnisignal.knowledge.release import KnowledgeError, _lock

from .analysis import MAX_BATCH_ITEMS

ACTOR = "system:daily-pilot"
CONFIG_ENV = "OMNISIGNAL_WORKBENCH_DAILY_PILOT_CONFIG"
# Default trial bounds; the persisted policy may widen them within MAX_TARGET
# bounds so the scheduler is no longer tied to a single hardcoded week.
FIRST_DAY, LAST_DAY = date(2026, 10, 27), date(2026, 11, 2)
MAX_HORIZON_DAYS = 730
# 100 items is a per-request batch size, not an operating ceiling: plans are chunked
# internally (MAX_BATCH_ITEMS per analysis batch), so a larger target is safe.
MAX_TARGET = 2000
# One day's plan never exceeds target/max-batch chunks; the bound keeps the
# orphan scan deterministic without iterating an unbounded range.
MAX_CHUNKS = (MAX_TARGET // MAX_BATCH_ITEMS) + 2
# Retain the whole bounded operating window, including a daily policy's weekends.
# Item identifiers can contain escaped Unicode, and appear in items and projects.
MAX_STATE_FILES = MAX_HORIZON_DAYS + 1
MAX_STATE_BYTES = 16000 + MAX_TARGET * 2048
# Operating policies use Shanghai's fixed UTC+08 day boundary.
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_SCAN = 10000
POLICY_KEYS = {"start_date", "end_date", "timezone", "weekdays", "hour", "minute",
               "target", "review_target_min", "review_target_max"}
STATE_KEYS = {"version", "day", "actor", "request_id", "policy", "items", "projects",
              "preview", "phase", "created_at", "checksum"}
REASONS = {"missing", "revision_changed", "not_pending", "permission_required", "already_running",
           "already_analyzed", "retry_required", "not_configured", "disabled", "budget_exhausted", "queue_full"}


class PilotError(Exception):
    """Only fixed diagnostic codes cross the status boundary."""


def _check(value, code="invalid_state"):
    if not value:
        raise PilotError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result)
        result[key] = value
    return result


def _json(path, maximum):
    _check(not path.is_symlink() and path.is_file() and path.stat().st_size <= maximum)
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)


def _policy(value):
    _check(isinstance(value, dict) and set(value) == POLICY_KEYS, "invalid_policy")
    # A wider window is an operational choice, not a contract violation: any
    # bounded ISO range in the Shanghai day plane is accepted so the trial can be
    # extended without a code change. It must contain no implicit drift.
    _check(value["timezone"] == "Asia/Shanghai", "invalid_policy")
    try:
        start = date.fromisoformat(value["start_date"])
        end = date.fromisoformat(value["end_date"])
    except (TypeError, ValueError):
        raise PilotError("invalid_policy") from None
    _check(start <= end, "invalid_policy")
    _check((end - start).days <= MAX_HORIZON_DAYS, "invalid_policy")
    weekdays = value["weekdays"]
    _check(isinstance(weekdays, list) and 1 <= len(weekdays) <= 7
           and all(type(item) is int and 0 <= item <= 6 for item in weekdays)
           and weekdays == sorted(set(weekdays)), "invalid_policy")
    _check(type(value["hour"]) is int and value["hour"] == 16
           and type(value["minute"]) is int and value["minute"] == 0, "invalid_policy")
    _check(type(value["target"]) is int and 1 <= value["target"] <= MAX_TARGET, "invalid_policy")
    _check(type(value["review_target_min"]) is int and value["review_target_min"] == 10
           and type(value["review_target_max"]) is int and value["review_target_max"] == 20, "invalid_policy")
    return deepcopy(value)


def _request(day):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:omnisignal:daily-pilot:v1:" + day))


def _atomic(path, value):
    """Persist the intent before enqueue, including the directory rename on POSIX."""
    _check(not path.is_symlink())
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class DailyPilot:
    def __init__(self, store, analysis, *, policy_path=None):
        self.store, self.analysis = store, analysis
        self.policy_path = policy_path if policy_path is not None else os.getenv(CONFIG_ENV, "")
        self.root = store.root / "operations" / "daily-pilot"
        self._stop = threading.Event()
        self._guard = threading.RLock()
        self._thread = None
        self._view = {"enabled": bool(self.policy_path), "state": "not_started" if self.policy_path else "disabled",
                      "diagnostic": None, "policy": None, "current_day": None, "report": None, "recent": []}

    def snapshot(self):
        """Purely cached status: calling this method never queries, plans or enqueues."""
        with self._guard:
            return deepcopy(self._view)

    def _save(self, state):
        state["checksum"] = _hash({key: value for key, value in state.items() if key != "checksum"})
        _atomic(self.root / ("intent-" + state["day"] + ".json"), state)

    def _read_states(self):
        states = {}
        files = list(self.root.glob("intent-*.json"))
        _check(len(files) <= MAX_STATE_FILES)
        for path in files:
            value = _json(path, MAX_STATE_BYTES)
            _check(isinstance(value, dict) and set(value) == STATE_KEYS)
            _check(value["checksum"] == _hash({k: v for k, v in value.items() if k != "checksum"}))
            day = date.fromisoformat(value["day"])
            stored = _policy(value["policy"])
            _check(date.fromisoformat(stored["start_date"]) <= day <= date.fromisoformat(stored["end_date"])
                   and day.weekday() in stored["weekdays"])
            _check(path.name == "intent-" + day.isoformat() + ".json")
            _check(value["version"] == 1 and type(value["version"]) is int and value["actor"] == ACTOR)
            _check(value["request_id"] == _request(value["day"]))
            policy = stored
            items = value["items"]
            _check(isinstance(items, list) and len(items) <= policy["target"])
            if items:
                for offset in range(0, len(items), MAX_BATCH_ITEMS):
                    chunk = items[offset:offset + MAX_BATCH_ITEMS]
                    _check(self.analysis.batch_items(chunk) == chunk)
                _check(len({item["asset_id"] for item in items}) == len(items))
            _check(isinstance(value["projects"], dict) and set(value["projects"]) == {i["asset_id"] for i in items})
            _check(all(isinstance(v, str) and len(v) == 64 for v in value["projects"].values()))
            preview = value["preview"]
            _check(isinstance(preview, dict) and set(preview) == {"pending", "examined", "skip_reasons", "scan_limited"})
            _check(all(type(preview[k]) is int and preview[k] >= 0 for k in ("pending", "examined")))
            _check(preview["examined"] <= min(MAX_SCAN, preview["pending"]) and type(preview["scan_limited"]) is bool)
            _check(isinstance(preview["skip_reasons"], dict) and set(preview["skip_reasons"]) <= REASONS
                   and all(type(v) is int and v > 0 for v in preview["skip_reasons"].values()))
            _check(sum(preview["skip_reasons"].values()) + len(items) == preview["examined"])
            _check(value["phase"] in {"prepared", "observed", "empty", "expired"})
            _check((value["phase"] == "empty") == (not items))
            created = datetime.fromisoformat(value["created_at"])
            _check(created.tzinfo is not None and created.astimezone(SHANGHAI).date() == day
                   and created.astimezone(SHANGHAI).hour >= policy["hour"])
            states[value["day"]] = value
        return states

    def _plan(self, policy, now):
        # One read snapshot, then round robin by project. Only the existing
        # analysis preflight decides permissions, previous work and quota.
        with self.store.db() as db:
            db.execute("BEGIN")
            pending = db.execute("SELECT count(*) FROM assets WHERE status='pending'").fetchone()[0]
            rows = db.execute("SELECT document FROM assets WHERE status='pending' ORDER BY created_at,id LIMIT ?", (MAX_SCAN,)).fetchall()
        groups, project_ids = {}, {}
        for row in rows:
            asset = json.loads(row[0])
            project = asset.get("project_id") or asset.get("source_url") or asset["id"]
            project_ids[asset["id"]] = _hash(project)
            groups.setdefault(project, deque()).append({"asset_id": asset["id"], "expected_revision": asset["revision"]})
        order, queue = [], deque(groups.values())
        while queue:
            group = queue.popleft()
            order.append(group.popleft())
            if group:
                queue.append(group)
        items, skipped, examined = [], Counter(), 0
        for offset in range(0, len(order), MAX_BATCH_ITEMS):
            preview = self.analysis.preview_batch(order[offset:offset + MAX_BATCH_ITEMS])
            for item in preview["items"]:
                examined += 1
                if item["disposition"] == "eligible":
                    items.append({key: item[key] for key in ("asset_id", "expected_revision")})
                else:
                    _check(item["reason_code"] in REASONS)
                    skipped[item["reason_code"]] += 1
                if len(items) == policy["target"]:
                    break
            if len(items) == policy["target"]:
                break
        return {"version": 1, "day": now.date().isoformat(), "actor": ACTOR,
                "request_id": _request(now.date().isoformat()), "policy": deepcopy(policy), "items": items,
                "projects": {item["asset_id"]: project_ids[item["asset_id"]] for item in items},
                "preview": {"pending": pending, "examined": examined, "skip_reasons": dict(skipped),
                            "scan_limited": pending > MAX_SCAN},
                "phase": "prepared" if items else "empty", "created_at": now.isoformat(), "checksum": ""}

    def _batch(self, state):
        try:
            return self.analysis.batch(state["request_id"], ACTOR)
        except Exception:
            raise PilotError("batch_lookup_failed") from None

    def _chunks(self, state):
        ids = self._chunk_ids(state["day"])
        return [(ids[index], state["items"][offset:offset + MAX_BATCH_ITEMS])
                for index, offset in enumerate(range(0, len(state["items"]), MAX_BATCH_ITEMS))]

    def _read_batches(self, state):
        batches = []
        for request_id, expected in self._chunks(state):
            batch = self._batch({"request_id": request_id})
            if batch is not None:
                # A durable receipt is not enough: it must describe this exact
                # frozen chunk, including skipped items and their revisions.
                actual = [{key: item.get(key) for key in ("asset_id", "expected_revision")}
                          for item in batch.get("items", [])]
                _check(batch.get("batch_id") == uuid.UUID(request_id).hex and actual == expected,
                       "batch_payload_mismatch")
            batches.append(batch)
        return batches

    @staticmethod
    def _combined(state, batches):
        present = [batch for batch in batches if batch is not None]
        if not present:
            return None
        totals = Counter()
        for batch in present:
            totals.update(batch["totals"])
        complete = len(present) == len(batches)
        status = ("running" if totals["queued"] + totals["running"] else "finished") if complete else (
            "expired_intent" if state["phase"] == "expired" else "submission_uncertain")
        return {"batch_id": present[0]["batch_id"], "state": status, "totals": dict(totals),
                "items": [item for batch in present for item in batch["items"]]}

    def _report(self, state, batch, now):
        totals = batch["totals"] if batch else {}
        submitted = totals.get("submitted", 0)
        completed, failed = totals.get("completed", 0), totals.get("failed", 0)
        skips = Counter(state["preview"]["skip_reasons"])
        for item in (batch or {}).get("items", []):
            if item["disposition"] == "skipped":
                skips[item["reason_code"]] += 1
        completed_items = [i for i in (batch or {}).get("items", []) if i["status"] == "completed"]
        review_pending = 0
        with self.store.db() as db:
            for item in completed_items:
                row = db.execute("SELECT document FROM assets WHERE id=?", (item["asset_id"],)).fetchone()
                asset = json.loads(row[0]) if row else {}
                if (asset.get("status") == "pending" and asset.get("revision") == item["expected_revision"]
                        and asset.get("analysis_suggestion_revision") == item["expected_revision"]
                        and (asset.get("reviewed_analysis") or {}).get("source_job_id") != item["job_id"]):
                    review_pending += 1
        phase = batch["state"] if batch else "no_eligible" if not state["items"] else "expired_intent" if state["phase"] == "expired" else "submission_uncertain"
        report = {"day": state["day"], "state": phase, "batch_id": batch["batch_id"] if batch else None,
                  "planned_images": state["policy"]["target"], "eligible_at_plan": len(state["items"]),
                  "submitted": submitted, "analysed_images": completed, "failed": failed,
                  "queued": totals.get("queued", 0), "running": totals.get("running", 0),
                  "projects_covered": len({state["projects"][i["asset_id"]] for i in completed_items}),
                  "submitted_projects": len({state["projects"][i["asset_id"]] for i in (batch or {}).get("items", []) if i["disposition"] == "submitted"}),
                  "skip_reasons": dict(skips), "pending_at_plan": state["preview"]["pending"],
                  "examined": state["preview"]["examined"], "scan_limited": state["preview"]["scan_limited"],
                  "submission_shortfall": max(0, state["policy"]["target"] - submitted),
                  "analysis_shortfall": max(0, state["policy"]["target"] - completed),
                  "requires_human_review": review_pending, "review_target_min": state["policy"]["review_target_min"],
                  "review_target_max": state["policy"]["review_target_max"], "reviewed_count": None,
                  "updated_at": now.isoformat()}
        _atomic(self.root / ("report-" + state["day"] + ".json"), report)
        return report

    def _enqueue_missing(self, state, batches):
        """Only absent receipts are submitted, using the original frozen IDs.

        A lost response stops this pass. Reconciliation reads the receipts before
        any later pass can submit the remaining chunks; existing jobs never retry.
        """
        for (request_id, chunk), batch in zip(self._chunks(state), batches):
            if batch is None:
                self.analysis.enqueue_batch(request_id, chunk, ACTOR)

    @staticmethod
    def _chunk_ids(day):
        """Deterministic batch ids for one day; chunk 0 keeps the legacy id."""
        head = _request(day)
        return [head] + [
            str(uuid.uuid5(uuid.NAMESPACE_URL,
                           "urn:omnisignal:daily-pilot:chunk:%s:%d" % (day, index)))
            for index in range(1, MAX_CHUNKS)]

    def _reconcile(self, state, now, allow_submit):
        batches = self._read_batches(state)
        complete = all(batch is not None for batch in batches)
        if not complete and state["phase"] == "observed":
            raise PilotError("observed_batch_missing")
        if not complete and state["phase"] == "prepared":
            if allow_submit:
                try:
                    self._enqueue_missing(state, batches)
                except Exception:
                    # The receipt may already exist. Read all chunks; never make
                    # a second submission from this exception path.
                    batches = self._read_batches(state)
                    if any(batch is None for batch in batches):
                        report = self._report(state, self._combined(state, batches), now)
                        if state["day"] == now.date().isoformat():
                            self._view["report"] = report
                        raise PilotError("submission_uncertain") from None
                else:
                    batches = self._read_batches(state)
                    if any(batch is None for batch in batches):
                        report = self._report(state, self._combined(state, batches), now)
                        if state["day"] == now.date().isoformat():
                            self._view["report"] = report
                        raise PilotError("submission_uncertain")
            elif state["day"] < now.date().isoformat():
                state["phase"] = "expired"
                self._save(state)
        if batches and all(batch is not None for batch in batches) and state["phase"] == "prepared":
            state["phase"] = "observed"
            self._save(state)
        return self._report(state, self._combined(state, batches), now)

    def _check_orphans(self, start_day, day, states):
        # A missing intent must not silently become a new selection. Check the
        # entire bounded date range, including days enabled by an earlier policy.
        # A few indexed SQL reads avoid opening thousands of batch connections
        # when an operator first starts a long-running policy late in its window.
        ids = []
        for offset in range((day - start_day).days + 1):
            candidate = start_day + timedelta(days=offset)
            if candidate.isoformat() not in states:
                ids.extend(uuid.UUID(value).hex for value in self._chunk_ids(candidate.isoformat()))
        with self.store.db() as db:
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                found = db.execute("SELECT 1 FROM analysis_batches WHERE actor=? AND id IN ("
                                   + ",".join("?" for _ in chunk) + ") LIMIT 1", (ACTOR, *chunk)).fetchone()
                _check(found is None, "batch_without_intent")

    def tick(self, now=None):
        with self._guard:
            if not self.policy_path:
                return self.snapshot()
            now = now or datetime.fromtimestamp(self.store.clock(), timezone.utc)
            try:
                _check(isinstance(now, datetime) and now.tzinfo is not None, "invalid_clock")
                now = now.astimezone(SHANGHAI)
                self._view.update(current_day=now.date().isoformat(), diagnostic=None)
                self._view["report"] = next((r for r in self._view["recent"] if r["day"] == now.date().isoformat()), None)
                try:
                    policy = _policy(_json(Path(self.policy_path), 16000))
                except Exception:
                    raise PilotError("invalid_policy") from None
                self._view["policy"] = policy
                for path in (self.store.root / "operations", self.root):
                    _check(not path.is_symlink(), "invalid_state")
                    path.mkdir(exist_ok=True)
                with _lock(self.root):
                    try:
                        states = self._read_states()
                    except Exception:
                        raise PilotError("invalid_state") from None
                    day = now.date()
                    start_day = date.fromisoformat(policy["start_date"])
                    end_day = date.fromisoformat(policy["end_date"])
                    hour_ok = (now.hour, now.minute) >= (policy["hour"], policy["minute"])
                    allowed = start_day <= day <= end_day and day.weekday() in policy["weekdays"] and hour_ok
                    reports = []
                    # Recover every persisted intent before creating today's.
                    for key, state in sorted(states.items()):
                        _check(key <= day.isoformat(), "clock_before_saved_state")
                        if key == day.isoformat():
                            _check(state["policy"] == policy, "policy_changed_after_intent")
                        reports.append(self._reconcile(state, now, allowed and key == day.isoformat()))
                    if allowed and day.isoformat() not in states:
                        self._check_orphans(start_day, day, states)
                        state = self._plan(policy, now)
                        self._save(state)
                        reports.append(self._reconcile(state, now, True))
                    current = next((r for r in reports if r["day"] == day.isoformat()), None)
                    status = "ended" if day > end_day else "before_period" if day < start_day else "weekend" if day.weekday() not in policy["weekdays"] else current["state"] if current else "waiting_window"
                    self._view.update(state=status, report=current, recent=list(reversed(reports)))
            except PilotError as error:
                self._view.update(state="blocked", diagnostic=str(error))
            except KnowledgeError:
                self._view.update(state="blocked", diagnostic="pilot_lock_busy")
            except Exception:
                self._view.update(state="blocked", diagnostic="pilot_operation_failed")
            return self.snapshot()

    def start(self):
        """The app must call this only while owning its existing worker OS lock."""
        if not self.policy_path or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        def work():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(30)
        self._thread = threading.Thread(target=work, name="omni-daily-pilot", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
