"""Explicit rebaseline planning for a separate, recipient-bound library feed.

No source edits, paid analysis or history copying. Initialization only freezes
the operator's chosen target; the existing worker signs fresh eligible cases.
"""
from collections import Counter
import json
import os
from pathlib import Path

from omnisignal.knowledge.release import _atomic_json, _file, _lock
from omnisignal.knowledge.scope import recipient_key, validate_library_recipient
from .publication_use import digest, library_recipient, permits_internal_delivery, validate_policy
from .store import asset_from_row, require, stamp


def base_root(store, override=None):
    return Path(override or os.getenv("OMNISIGNAL_KNOWLEDGE_DIR", str(store.root / "published")))


def library_root(base, recipient):
    # Keep Windows fixture/worktree paths below ordinary MAX_PATH. The marker
    # still compares the complete recipient, so a prefix collision fails shut.
    return _file(Path(base), "libraries/" + recipient_key(recipient)[:24])


def publication_targets(base):
    """Discover explicitly initialized namespaces; never infer a new audience."""
    base = Path(base)
    targets = [(base, None)]
    directory = _file(base, "libraries")
    if directory.exists():
        require(directory.is_dir(), "invalid_library_directory")
        for entry in sorted(directory.iterdir()):
            require(not entry.is_symlink(), "invalid_library_directory")
            marker = _file(base, "libraries/" + entry.name + "/.workbench/library-target.json")
            if not marker.exists():
                continue
            require(marker.stat().st_size <= 16000, "invalid_library_target")
            value = json.loads(marker.read_text(encoding="utf-8"))
            recipient = validate_library_recipient(value.get("recipient"))
            require(entry.name == recipient_key(recipient)[:24], "invalid_library_target")
            targets.append((entry, recipient))
    return targets


def target_root(store, policy, override=None):
    base = base_root(store, override)
    recipient = library_recipient(policy)
    return library_root(base, recipient) if recipient is not None else base


def require_library_target(root, policy):
    recipient = library_recipient(policy)
    require(recipient is not None, "library_publication_policy_required")
    marker = _file(Path(root), ".workbench/library-target.json")
    require(marker.is_file() and marker.stat().st_size <= 16000, "library_rebaseline_not_initialized")
    value = json.loads(marker.read_text(encoding="utf-8"))
    require(value.get("recipient") == recipient and value.get("policy_sha256") == digest(policy), "library_rebaseline_policy_changed")
    return value


def library_rebaseline_plan(store, policy, *, publication_root=None):
    from .review import validate_automated_quality
    from .taxonomy import fresh_taxonomy
    policy = validate_policy(policy)
    recipient = library_recipient(policy)
    require(recipient is not None, "library_publication_policy_required")
    selected, reasons = [], Counter()
    with store.db() as db:
        for row in db.execute("SELECT * FROM assets ORDER BY id"):
            try:
                asset = asset_from_row(row, strict=True)
                require(asset.get("status") == "accepted" and asset.get("publication_status") != "withdrawn", "not_current_accepted")
                require(asset.get("source_id") in policy["source_ids"] and asset.get("permissions", {}).get("analysis") == "allowed"
                        and permits_internal_delivery(asset, policy), "outside_library_use_scope")
                validate_automated_quality(db, asset)
                require(fresh_taxonomy(asset) is not None, "taxonomy_missing")
                selected.append({"id": asset["id"], "revision": asset["revision"], "sha256": asset["sha256"],
                                 "input_sha256": digest({k: v for k, v in asset.items() if k not in {"publication_status", "publication_version"}})})
            except (ValueError, KeyError, TypeError):
                # No model/provider output, source text or inferred right is returned.
                reasons["not_eligible_for_current_library_policy"] += 1
    value = {"schema_version": 1, "recipient": recipient, "policy_sha256": digest(policy), "assets": selected}
    return {**value, "plan_sha256": digest(value), "eligible_count": len(selected), "blocked": dict(reasons),
            "read_only": True, "model_calls": 0, "scope_migration": "new_feed_only"}


def initialize_library_feed(store, policy, expected_plan_sha256, *, publication_root=None):
    """Explicit local activation step; does not sign or submit a job."""
    policy = validate_policy(policy)
    plan = library_rebaseline_plan(store, policy, publication_root=publication_root)
    require(plan["plan_sha256"] == expected_plan_sha256, "library_rebaseline_plan_changed")
    root = target_root(store, policy, publication_root)
    with _lock(root / ".workbench"):
        marker = _file(root, ".workbench/library-target.json")
        if marker.exists():
            return require_library_target(root, policy)
        require(not (root / "latest.json").exists() and not (root / "releases").exists()
                and not (root / "withdrawals").exists(), "library_target_must_be_empty")
        value = {"schema_version": 1, "recipient": plan["recipient"], "policy_sha256": plan["policy_sha256"],
                 "rebaseline_plan_sha256": expected_plan_sha256, "initialized_at": stamp(),
                 "initial_eligible_count": plan["eligible_count"], "authorization_source": "operator_policy"}
        _atomic_json(marker, value)
    return value
