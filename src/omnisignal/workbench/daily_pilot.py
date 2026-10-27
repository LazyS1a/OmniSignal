"""Opt-in first-week scheduling; no collection, permission, review or publication writes."""
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

ACTOR = "system:daily-pilot"
CONFIG_ENV = "OMNISIGNAL_WORKBENCH_DAILY_PILOT_CONFIG"
FIRST_DAY, LAST_DAY = date(2026, 10, 27), date(2026, 11, 2)
# This deliberately finite trial is entirely within Shanghai's UTC+08 period.
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
    _check(value["start_date"] == FIRST_DAY.isoformat() and value["end_date"] == LAST_DAY.isoformat()
           and value["timezone"] == "Asia/Shanghai", "invalid_policy")
    _check(value["weekdays"] == [0, 1, 2, 3, 4]
           and all(type(item) is int for item in value["weekdays"]), "invalid_policy")
    _check(type(value["hour"]) is int and value["hour"] == 16
           and type(value["minute"]) is int and value["minute"] == 0, "invalid_policy")
    _check(type(value["target"]) is int and 1 <= value["target"] <= 100, "invalid_policy")
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
        _check(len(files) <= 7)
        for path in files:
            value = _json(path, 128000)
            _check(isinstance(value, dict) and set(value) == STATE_KEYS)
            _check(value["checksum"] == _hash({k: v for k, v in value.items() if k != "checksum"}))
            day = date.fromisoformat(value["day"])
            _check(FIRST_DAY <= day <= LAST_DAY and day.weekday() < 5)
            _check(path.name == "intent-" + day.isoformat() + ".json")
            _check(value["version"] == 1 and type(value["version"]) is int and value["actor"] == ACTOR)
            _check(value["request_id"] == _request(value["day"]))
            policy = _policy(value["policy"])
            items = value["items"]
            _check(isinstance(items, list) and len(items) <= policy["target"])
            if items:
                _check(self.analysis.batch_items(items) == items)
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
        for offset in range(0, len(order), 100):
            preview = self.analysis.preview_batch(order[offset:offset + 100])
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

    def _reconcile(self, state, now, allow_submit):
        batch = self._batch(state) if state["items"] else None
        if batch is None and state["phase"] == "observed":
            raise PilotError("observed_batch_missing")
        if batch is None and state["phase"] == "prepared":
            if allow_submit:
                try:
                    batch = self.analysis.enqueue_batch(state["request_id"], state["items"], ACTOR)
                except Exception:
                    # Lost response after a durable commit: retry only the read
                    # here. A later tick may replay exactly the persisted intent.
                    batch = self._batch(state)
                    if batch is None:
                        self._report(state, None, now)
                        raise PilotError("submission_uncertain") from None
            elif state["day"] < now.date().isoformat():
                state["phase"] = "expired"
                self._save(state)
        if batch is not None and state["phase"] != "observed":
            state["phase"] = "observed"
            self._save(state)
        return self._report(state, batch, now)

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
                    allowed = FIRST_DAY <= day <= LAST_DAY and day.weekday() in policy["weekdays"] and (now.hour, now.minute) >= (policy["hour"], policy["minute"])
                    reports = []
                    # Recover every persisted intent before creating today's.
                    for key, state in sorted(states.items()):
                        _check(key <= day.isoformat(), "clock_before_saved_state")
                        if key == day.isoformat():
                            _check(state["policy"] == policy, "policy_changed_after_intent")
                        reports.append(self._reconcile(state, now, allowed and key == day.isoformat()))
                    if allowed and day.isoformat() not in states:
                        # If a file was lost, the deterministic batch ID must not
                        # silently become a second, differently selected request.
                        for offset in range(7):
                            candidate = FIRST_DAY + timedelta(days=offset)
                            if candidate <= day and candidate.weekday() < 5 and candidate.isoformat() not in states:
                                orphan = self._batch({"request_id": _request(candidate.isoformat())})
                                _check(orphan is None, "batch_without_intent")
                        state = self._plan(policy, now)
                        self._save(state)
                        reports.append(self._reconcile(state, now, True))
                    current = next((r for r in reports if r["day"] == day.isoformat()), None)
                    status = "ended" if day > LAST_DAY else "before_period" if day < FIRST_DAY else "weekend" if day.weekday() not in policy["weekdays"] else current["state"] if current else "waiting_window"
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
