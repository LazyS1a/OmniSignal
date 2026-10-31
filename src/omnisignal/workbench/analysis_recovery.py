"""Explicit, offline recovery of a proven legacy revision/projection mismatch.

This is not a startup migration or an API endpoint. Operators first obtain a
read-only plan against a frozen pre-job SQLite backup, stop the old worker, then
apply that exact plan. No model, review, publication or permission calls occur.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from omnisignal.knowledge.release import _lock
from .analysis import ANALYSIS_PROJECTION_FIELDS, MAX_IMAGE_BYTES, _result, analysis_input_binding, job_matches_asset
from .store import Conflict, Store, asset_row_consistent, encode, require


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _editorial(asset):
    return {key: value for key, value in asset.items() if key not in ANALYSIS_PROJECTION_FIELDS}


def _audit(db, asset_id):
    return [dict(row) for row in db.execute("SELECT * FROM audit WHERE entity=? ORDER BY id", (asset_id,))]


def _candidate(store, db, baseline, asset_id, baseline_sha):
    row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    before = baseline.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    require(row is not None and before is not None, "missing_snapshot_asset")
    asset, original = json.loads(row["document"]), json.loads(before["document"])
    job_row = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (asset.get("analysis_job_id"),)).fetchone()
    require(job_row is not None, "missing_bound_job")
    job = dict(job_row)
    for name in ("input_binding", "asset_revision", "projection_evidence"):
        job.setdefault(name, None)
    if job.get("projection_evidence"):
        proof = json.loads(job["projection_evidence"])
        require(asset_row_consistent(row) and proof.get("baseline_sha256") == baseline_sha
                and job_matches_asset(job, asset), "recovered_state_changed")
        return {"asset_id": asset_id, "job_id": job["id"], "status": "already_recovered",
                "plan_sha256": proof.get("plan_sha256")}, None
    require(job.get("asset_revision") is None and not job.get("input_binding"), "not_legacy_unbound_job")
    require(type(asset.get("revision")) is int and row["id"] == before["id"] == asset["id"] == original["id"]
            and row["revision"] == before["revision"] == asset["revision"] + 1
            and original["revision"] == asset["revision"], "not_exact_legacy_revision_drift")
    require(row["status"] == before["status"] == asset.get("status") == original.get("status") == "pending"
            and not asset.get("reviewed_analysis") and not asset.get("reviewed_taxonomy")
            and asset.get("publication_status") not in {"published", "withdrawn"}, "review_or_publication_exists")
    require(_editorial(asset) == _editorial(original), "editorial_input_changed_since_snapshot")
    require(asset.get("permissions", {}).get("analysis") == "allowed", "analysis_permission_not_allowed")
    require(job["asset_id"] == asset_id and job["revision"] == asset["revision"]
            and job_matches_asset(job, asset), "job_identity_changed")
    require(job["status"] in {"queued", "waiting_remote", "completed"},
            "running_job_must_finish" if job["status"] == "running" else "failed_or_stale_job_not_retried")
    require(job["charged"] == (0 if job["status"] == "queued" else 1), "charge_state_mismatch")
    if baseline.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_jobs'").fetchone():
        require(not baseline.execute("SELECT 1 FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone(), "job_not_newer_than_snapshot")
    prior_audit, current_audit = _audit(baseline, asset_id), _audit(db, asset_id)
    require(prior_audit and current_audit[:len(prior_audit)] == prior_audit, "audit_prefix_changed")
    later = current_audit[len(prior_audit):]
    allowed = {"analysis_enqueue", "analysis_completed", "analysis_failed", "analysis_waiting_remote", "analysis_stale", "analysis_interrupted"}
    require(later and all(item["action"] in allowed for item in later), "non_analysis_edit_since_snapshot")
    enqueue = [item for item in later if item["action"] == "analysis_enqueue"
               and json.loads(item["details"]).get("job_id") == job["id"]]
    require(len(enqueue) == 1 and json.loads(enqueue[0]["details"]).get("revision") == job["revision"]
            and enqueue[0]["at"] >= prior_audit[-1]["at"] and job["created_at"] >= prior_audit[-1]["at"], "enqueue_evidence_missing")
    image_path = store.images / asset["image_file"]
    require(image_path.parent.resolve() == store.images.resolve() and image_path.is_file()
            and not image_path.is_symlink() and image_path.stat().st_size <= MAX_IMAGE_BYTES
            and hashlib.sha256(image_path.read_bytes()).hexdigest() == asset["sha256"], "image_binding_changed")
    suggestion = None
    if job["status"] == "completed":
        suggestion = json.loads(job["suggestion"])
        require(isinstance(suggestion, dict), "invalid_completed_result")
        clean = {key: value for key, value in suggestion.items() if key not in {"source_image_id", "provenance", "model_version"}}
        model = suggestion.get("model_version")
        require(isinstance(model, str) and model.strip() and len(model) <= 200
                and _result(clean, asset_id, require_taxonomy=True) == {key: value for key, value in suggestion.items() if key != "model_version"}, "invalid_completed_result")
    evidence = {"asset_id": asset_id, "job_id": job["id"], "status": "eligible", "job_status": job["status"],
                "source_revision": job["revision"], "asset_revision": row["revision"],
                "baseline_sha256": baseline_sha, "baseline_document_sha256": digest(original),
                "current_document_sha256": digest(asset), "job_sha256": digest(job),
                "audit_sha256": digest(current_audit), "image_sha256": asset["sha256"]}
    return evidence, (row, asset, job, suggestion)


def recover_analysis_projections(store, baseline_path, asset_ids, *, apply=False, expected_plan_sha256=None,
                                 actor="maintenance:analysis-projection"):
    """Plan first; apply exact, explicit eligible IDs atomically and idempotently.

    A worker holds .analysis-worker for the entire paid request, so a running
    request is never repaired in flight. Queued/waiting jobs keep their original
    IDs, cache keys and billing state; completed jobs reuse the stored result.
    """
    require(isinstance(asset_ids, list) and 0 < len(asset_ids) <= 500 and len(set(asset_ids)) == len(asset_ids)
            and all(isinstance(item, str) and 0 < len(item) <= 128 for item in asset_ids), "invalid_recovery_selection")
    require(type(apply) is bool and isinstance(actor, str) and 0 < len(actor) <= 200, "invalid_recovery_actor")
    baseline_path = Path(baseline_path)
    require(baseline_path.is_file() and not baseline_path.is_symlink()
            and baseline_path.resolve() != store.path.resolve(), "frozen_backup_required")
    baseline_sha = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    with _lock(store.root / ".analysis-worker") if apply else nullcontext():
        baseline = sqlite3.connect(baseline_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        baseline.row_factory = sqlite3.Row
        try:
            with store.db() as db:
                db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
                if apply:
                    # Same additive schema as AnalysisService, in this recovery
                    # transaction. This permits maintenance before starting the
                    # new worker, without letting old drifted queued jobs run.
                    columns = {row[1] for row in db.execute("PRAGMA table_info(analysis_jobs)")}
                    for name, kind in (("input_binding", "TEXT"), ("asset_revision", "INTEGER"), ("projection_evidence", "TEXT")):
                        if name not in columns:
                            db.execute(f"ALTER TABLE analysis_jobs ADD COLUMN {name} {kind}")
                items, eligible = [], []
                for asset_id in sorted(asset_ids):
                    try:
                        evidence, state = _candidate(store, db, baseline, asset_id, baseline_sha)
                        items.append(evidence)
                        if state:
                            eligible.append((evidence, state))
                    except (Conflict, KeyError, TypeError, ValueError, OSError) as exc:
                        # Only fixed gate messages, never arbitrary SQLite rows,
                        # file paths, provider errors, or source text enter reports.
                        reason = str(exc) if type(exc) is Conflict else "invalid_recovery_evidence"
                        items.append({"asset_id": asset_id, "status": "blocked", "reason": reason})
                plan = {"schema_version": 1, "baseline_sha256": baseline_sha, "items": items}
                plan_sha = digest(plan)
                if not apply:
                    return {**plan, "plan_sha256": plan_sha, "applied": False}
                if all(item["status"] == "already_recovered" and item["plan_sha256"] == expected_plan_sha256 for item in items):
                    return {**plan, "plan_sha256": expected_plan_sha256, "applied": False, "already_recovered": True}
                require(isinstance(expected_plan_sha256, str) and expected_plan_sha256 == plan_sha, "recovery_plan_changed")
                require(len(eligible) == len(items), "recovery_selection_contains_blocked_items")
                require(hashlib.sha256(baseline_path.read_bytes()).hexdigest() == baseline_sha, "backup_changed")
                for evidence, (row, asset, job, suggestion) in eligible:
                    asset["revision"] = row["revision"]
                    if suggestion is not None:
                        asset.update(analysis_suggestion=suggestion, analysis_suggestion_revision=row["revision"], analysis_status="analyzed")
                    else:
                        asset["analysis_status"] = "analyzing"
                    proof = {"source_revision": job["revision"], "asset_revision": row["revision"],
                             "evidence_sha256": digest(evidence), "baseline_sha256": baseline_sha, "plan_sha256": plan_sha}
                    require(db.execute("UPDATE assets SET document=? WHERE id=? AND revision=? AND document=?",
                                       (encode(asset), asset["id"], row["revision"], row["document"])).rowcount == 1, "recovery_asset_changed")
                    require(db.execute("UPDATE analysis_jobs SET asset_revision=?,input_binding=?,projection_evidence=? WHERE id=? AND asset_revision IS NULL AND input_binding IS NULL",
                                       (row["revision"], analysis_input_binding(asset), encode(proof), job["id"])).rowcount == 1, "recovery_job_changed")
                    store.audit(db, asset["id"], actor, "analysis_projection_recovered",
                                {**proof, "job_id": job["id"], "job_status": job["status"], "charged": bool(job["charged"]), "provider_calls": 0})
                return {**plan, "plan_sha256": plan_sha, "applied": True, "recovered": len(eligible), "provider_calls": 0}
        finally:
            baseline.close()


class _MaintenanceStore:
    """Do not initialize Store, start workers, or recover publications on a read."""
    audit = Store.audit

    def __init__(self, root, *, read_only):
        self.root = Path(root).resolve()
        self.images = self.root / "images"
        self.path = self.root / "workbench.sqlite3"
        require(self.path.is_file(), "workbench_database_missing")
        self.read_only = read_only

    @contextmanager
    def db(self):
        uri = self.path.as_uri() + ("?mode=ro" if self.read_only else "?mode=rw")
        db = sqlite3.connect(uri, uri=True, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan or apply a proven legacy analysis projection repair; defaults to read-only.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--baseline", required=True, help="Frozen pre-job SQLite backup")
    parser.add_argument("--asset-ids-file", required=True, help="JSON array of explicitly selected asset IDs")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--output", help="Optional private JSON report path")
    args = parser.parse_args(argv)
    if args.apply and not args.expected_plan_sha256:
        parser.error("--apply requires --expected-plan-sha256 from a reviewed dry-run plan")
    try:
        ids_path = Path(args.asset_ids_file)
        require(ids_path.is_file() and ids_path.stat().st_size <= 100000, "invalid_recovery_selection_file")
        asset_ids = json.loads(ids_path.read_text(encoding="utf-8-sig"))
        store = _MaintenanceStore(args.root, read_only=not args.apply)
        result = recover_analysis_projections(store, args.baseline, asset_ids, apply=args.apply,
                                             expected_plan_sha256=args.expected_plan_sha256)
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Conflict as exc:
        print(json.dumps({"applied": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"applied": False, "error": "recovery_unavailable_or_worker_busy"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
