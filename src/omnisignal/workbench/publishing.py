"""Publish reviewed assets through the existing schema-1 signed file protocol.

This adapter performs no network or model calls. Publication proves that a local
release was signed, never that an independently operated consumer received it.
Callers authenticate the actor and supply assets from their own editorial store.
"""
from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import uuid

from cryptography.hazmat.primitives import serialization
from PIL import Image

from omnisignal.knowledge.release import (
    KnowledgeError, MAX_IMAGE_BYTES, MAX_PACKAGE_BYTES, MAX_RECORDS, _identifier, _image, _lock,
    _require, _text, digest, load_private_key, make_record, publish_release,
    read_latest, _history, publish_withdrawals, verify_release, materialize_release, _withdrawn,
)
from .review import DESIGN_FIELDS, fresh_review
from .taxonomy import fresh_taxonomy
from .store import Conflict, encode, require, stamp
from omnisignal.knowledge.taxonomy import TAXONOMY_VERSION
PURPOSES = {"brand", "conversion"}
CHECKPOINT_CHAIN_LIMIT = 16


def publication_snapshot_ids(items):
    """Current and legacy stable IDs for the same frozen asset snapshots."""
    identity = [[item["id"], item["revision"], item["sha256"]] for item in sorted(items, key=lambda item: item["id"])]
    identity_hash = hashlib.sha256(encode(identity).encode()).hexdigest()
    return "wb-" + identity_hash[:32], "wb-" + identity_hash[:48]


def _verified_chain(root, key_id, private_key):
    latest = read_latest(root)
    release_id = latest["release_id"] if latest else None
    chain, seen = [], set()
    while release_id is not None:
        _require(release_id not in seen, "cyclic publication chain")
        seen.add(release_id)
        directory = root / "releases" / _identifier(release_id)
        manifest, records = verify_release(directory, {key_id: private_key.public_key()})
        _require(not chain or manifest["sequence"] == chain[-1][0]["sequence"] - 1, "broken publication chain")
        chain.append((manifest, records, directory))
        release_id = manifest["parent_release_id"] if manifest["kind"] == "delta" else None
    return chain


def _stage_records(records, locations, stage):
    """Copy only signed, hash-checked prior images, never synthesize old cases."""
    refs = set()
    for record in records:
        if record["op"] != "upsert":
            continue
        for entry in record["case"]["images"]:
            name = entry["path"]
            refs.add(name)
            target = stage / name
            if not target.exists():
                data = (locations[record["case_id"]] / name).read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    # Omitted/withdrawn earlier records must not leave unreferenced staged files.
    for path in (stage / "images").glob("*"):
        if path.relative_to(stage).as_posix() not in refs:
            path.unlink()
    _require(sum((stage / name).stat().st_size for name in refs) <= MAX_PACKAGE_BYTES,
             "publication checkpoint exceeds image budget")


def _source_url(value: object) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 8192, "source URL required")
    try:
        parsed = urlsplit(value)
        _require(parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None,
                 "source URL must be traceable HTTP(S) without credentials")
        parsed.port
    except ValueError as exc:
        raise KnowledgeError("invalid source URL") from exc
    return value


def _prepare_asset(asset: dict, stage: Path) -> dict:
    _require(isinstance(asset, dict), "invalid asset")
    asset_id = _identifier(asset.get("id"))
    _require(asset.get("status") == "accepted", "only accepted assets can be published")
    permissions = asset.get("permissions", {})
    _require(isinstance(permissions, dict), "invalid asset permissions")
    _require(all(permissions.get(name) == "allowed" for name in ("display", "export")),
             "publication requires allowed display and export permissions")
    permission_values = {name: permissions.get(name, "unknown")
                         for name in ("display", "analysis", "export")}
    _require(all(value in {"allowed", "unknown", "denied"}
                 for value in permission_values.values()), "invalid asset permission state")
    source_url = _source_url(asset.get("source_url"))
    image_source_url = asset.get("source_image_url")
    if image_source_url:
        image_source_url = _source_url(image_source_url)
    _text(asset.get("title"), "title")
    material_kind = asset.get("kind", "advertisement")
    _require(material_kind in {"advertisement", "reference"}, "reviewed material kind required")
    purposes = asset.get("purposes")
    _require(isinstance(purposes, list)
             and all(isinstance(value, str) and value in PURPOSES for value in purposes),
             "reviewed advertising purpose required")
    _require(material_kind != "advertisement" or bool(purposes), "reviewed advertising purpose required")
    revision = asset.get("revision")
    _require(type(revision) is int and revision > 0, "positive asset revision required")
    image_path = asset.get("image_path")
    _require(isinstance(image_path, (str, Path)) and bool(str(image_path)), "asset image path required")
    image_path = Path(image_path)
    _require(image_path.is_absolute() and image_path.is_file()
             and not any(path.is_symlink() for path in (image_path, *image_path.parents)),
             "asset image must be an absolute regular file without symlinks")
    _require(image_path.stat().st_size <= MAX_IMAGE_BYTES, "asset image exceeds size limit")
    content = image_path.read_bytes()
    _image(content)
    sha256 = digest(content)
    _require(asset.get("sha256") == sha256, "asset image hash changed; review it again")
    with Image.open(io.BytesIO(content)) as image:
        width, height = image.size
        extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[image.format]
    _require(asset.get("width") == width and asset.get("height") == height,
             "asset image dimensions changed; review it again")
    # Content-addressed names avoid duplicating a long asset ID and permit two
    # cases to share identical bytes in one package without Windows path bloat.
    relative = f"images/{sha256}.{extension}"
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)

    # Unreviewed descriptions and OCR remain annotations. Explicitly reviewed
    # design knowledge is bound to this exact current revision and image hash.
    notes = {}
    for name in ("description", "ocr_text", "reason"):
        value = asset.get(name) or ""
        _require(isinstance(value, str) and len(value) <= 20000, f"invalid {name}")
        notes[name] = value
    project_id = asset.get("project_id")
    _require(project_id is None or isinstance(project_id, str), "invalid project identifier")
    brand = asset.get("brand") or "未标注"
    _text(brand, "brand")
    try:
        reviewed = fresh_review(asset)
        taxonomy = fresh_taxonomy(asset)
    except Conflict as exc:
        raise KnowledgeError(str(exc)) from None
    _require(taxonomy is not None, "发布前请完成人工分类审核或明确标识的 AI 分类复核；旧素材不会自动补齐标签")
    case = {
        "case_id": asset_id, "revision": revision, "title": asset["title"],
        "summary_zh": ("人工审核收录的设计参考：" if material_kind == "reference" else "人工审核收录的广告素材：") + asset["title"],
        "brand_or_team": brand, "review_status": "approved",
        "display_permission": "allowed", "permissions": permission_values,
        "analysis_version": "manual-ad-review-1", "model_version": "none",
        "tags": taxonomy["tags"],
        "sources": [{"source_id": "source-1", "url": source_url,
                     "display_permission": "allowed", "permissions": permission_values}],
        "images": [{"image_id": "image-1", "source_id": "source-1", "path": relative,
                    "display_permission": "allowed", "permissions": permission_values,
                    "source_url": image_source_url, "width": width, "height": height}],
        "claims": [{"claim_id": "dimensions", "kind": "visual_observation",
                    "text": f"图片尺寸为 {width} × {height} 像素。",
                    "source_ids": [], "image_ids": ["image-1"]}],
        "editorial": {"provenance": "operator_supplied_not_model_verified",
                      "project_id": project_id, "material_kind": material_kind,
                      "marketing_purposes": sorted(set(purposes)),
                      "taxonomy_reviewed": True, "reviewed_taxonomy": taxonomy, **notes},
    }
    if reviewed is not None:
        case["summary_zh"] = reviewed["summary_zh"]
        case["model_version"] = reviewed["model_version"]
        case["analysis_version"] = ("assistant-design-review-1" if reviewed["provenance"] == "assistant_editorial_review"
                                    else "automated-design-review-1" if reviewed["provenance"] == "model_automated_review"
                                    else "human-design-review-1")
        case["claims"] = [
            {"claim_id": "design-" + field, "kind": reviewed["design"][field]["kind"],
             "text": reviewed["design"][field]["text"], "design_field": field,
             "source_ids": [], "image_ids": ["image-1"]}
            for field in DESIGN_FIELDS if reviewed["design"][field]["text"].strip()
        ]
        case["editorial"]["provenance"] = reviewed["provenance"]
        case["editorial"]["reviewed_analysis"] = reviewed
        if reviewed["provenance"] == "model_automated_review":
            case["editorial"]["review_method"] = "automated"
    return case


def publish_assets(root: Path, assets: list[dict], actor: str, *, publication_id=None) -> dict:
    """Publish all supplied assets or fail; retain prior releases via deltas.

    ``root`` is the publication root. The signing key must be supplied separately
    using OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE. An optional stable key ID is read
    from OMNISIGNAL_KNOWLEDGE_KEY_ID; otherwise a public-key fingerprint is used.
    B must trust that public key out of band. Existing ``nova-1`` vocabulary and
    schema 1 stay unchanged; advertising purposes are editorial metadata.
    """
    root = Path(root)
    if publication_id is not None:
        _identifier(publication_id)
    _text(actor, "authenticated publisher")
    _require(isinstance(assets, list) and 0 < len(assets) <= MAX_RECORDS,
             "select between 1 and 2000 accepted assets")
    key_file = os.environ.get("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", "").strip()
    _require(bool(key_file), "signing key is not configured")
    try:
        private_key = load_private_key(key_file, root)
    except (OSError, ValueError, TypeError) as exc:
        raise KnowledgeError("signing key configuration is invalid") from exc
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = os.environ.get("OMNISIGNAL_KNOWLEDGE_KEY_ID") or "ed25519-" + digest(public_bytes)[:16]
    _identifier(key_id)

    # The adapter lock serializes allocation of consecutive sequences while the
    # protocol's own lock still protects it from other CLI publishers.
    with tempfile.TemporaryDirectory(prefix="omni-publish-") as directory:
        stage = Path(directory)
        records, image_bytes = [], 0
        for asset in assets:
            record = make_record(_prepare_asset(asset, stage))
            records.append(record)
            image_bytes += (stage / record["case"]["images"][0]["path"]).stat().st_size
            _require(image_bytes <= MAX_PACKAGE_BYTES, "publication image budget exceeded")
        _require(len({record["case_id"] for record in records}) == len(records),
                 "duplicate asset in publication")
        with _lock(root / ".workbench"):
            latest = read_latest(root)
            sequence = 1 if latest is None else latest["sequence"] + 1
            release_id = publication_id or f"ad-{sequence}-{uuid.uuid4().hex[:12]}"
            kind, parent = "full" if latest is None else "delta", None if latest is None else latest["release_id"]
            requested_count = len(records)
            if publication_id is not None and (root / "releases" / release_id).exists():
                prior, prior_records = verify_release(root / "releases" / release_id, {key_id: private_key.public_key()})
                prior_by_id = {record["case_id"]: record for record in prior_records}
                _require(all(prior_by_id.get(record["case_id"]) == record for record in records), "publication recovery content conflict")
                _require(prior["kind"] == "full" or prior_records == records, "publication recovery content conflict")
                records = prior_records
                _stage_records(records, {record["case_id"]: root / "releases" / release_id for record in records}, stage)
                sequence, kind, parent = prior["sequence"], prior["kind"], prior["parent_release_id"]
            elif latest is not None:
                chain = _verified_chain(root, key_id, private_key)
                if len(chain) >= CHECKPOINT_CHAIN_LIMIT:
                    merged, locations = {}, {}
                    for _manifest, prior_records, directory in reversed(chain):
                        for record in prior_records:
                            if record["op"] == "delete":
                                merged.pop(record["case_id"], None)
                            else:
                                merged[record["case_id"]] = record
                                locations[record["case_id"]] = directory
                    for withdrawn in _withdrawn(root):
                        merged.pop(withdrawn, None)
                    for record in records:
                        merged[record["case_id"]] = record
                        locations[record["case_id"]] = stage
                    records = [merged[key] for key in sorted(merged)]
                    _require(len(records) <= MAX_RECORDS, "publication checkpoint exceeds record capacity")
                    _stage_records(records, locations, stage)
                    kind, parent = "full", None
            publish_release(
                root=root, release_id=release_id, sequence=sequence, records=records,
                asset_root=stage, private_key=private_key, key_id=key_id,
                kind=kind, parent_release_id=parent,
                taxonomy_version=TAXONOMY_VERSION,
            )
            manifest_sha256 = digest((root / "releases" / release_id / "manifest.json").read_bytes())
    return {"version": release_id, "release_id": release_id, "sequence": sequence,
            "status": "published", "receipt_status": "unconfirmed",
            "manifest_sha256": manifest_sha256, "assets_count": len(records),
            "requested_assets_count": requested_count, "kind": kind,
            "key_id": key_id, "taxonomy_version": TAXONOMY_VERSION}


def publish_store_assets(store, asset_ids, actor, *, expected_revisions=None,
                         automated_only=False, publication_root=None):
    """Same editorial transaction -> signature -> receipt order as the HTTP path.

    A stable snapshot ID replays an immutable signed directory after a crash
    before SQLite commit, including recovery of the release's latest pointer.
    The autonomous worker persists its batch first so later arrivals cannot
    change a previously signed but locally uncommitted operation's membership.
    """
    require(isinstance(asset_ids, list) and 1 <= len(asset_ids) <= 50
            and all(isinstance(value, str) for value in asset_ids)
            and len(set(asset_ids)) == len(asset_ids), "请明确选择 1–50 张素材，不可重复")
    require(not automated_only or actor == "system:auto-editorial", "invalid_automated_actor")
    root = Path(publication_root or os.getenv("OMNISIGNAL_KNOWLEDGE_DIR", str(store.root / "published")))
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        # A checkpoint carries the already published inventory forward. Refuse
        # to carry cases whose current source record lost its use basis.
        for case_id in materialize_release(root):
            prior_row = db.execute("SELECT document FROM assets WHERE id=?", (case_id,)).fetchone()
            require(prior_row is not None, "已发布素材的本地用途记录缺失，暂停发布")
            prior_asset = json.loads(prior_row[0])
            require(prior_asset.get("status") == "accepted" and prior_asset.get("publication_status") != "withdrawn"
                    and all(prior_asset.get("permissions", {}).get(name) == "allowed" for name in ("display", "export")),
                    "已发布素材用途状态已变化，需先处理撤回")
        selected = []
        for asset_id in sorted(asset_ids):
            row = db.execute("SELECT document FROM assets WHERE id=?", (asset_id,)).fetchone()
            require(row is not None, "选中的素材不存在")
            asset = json.loads(row[0])
            if expected_revisions is not None:
                require(expected_revisions.get(asset_id) == asset["revision"], "素材已被修改，请重新确认发布")
            if automated_only:
                reviewed, taxonomy = fresh_review(asset), fresh_taxonomy(asset)
                require(reviewed is not None and reviewed["provenance"] == "model_automated_review"
                        and taxonomy is not None and taxonomy.get("review_method") == "automated", "自动发布需要当前版本的 AI 分析和分类复核")
                require(asset.get("publication_status") != "withdrawn", "素材已永久撤回")
            asset["image_path"] = str(store.images / asset["image_file"])
            selected.append(asset)
        publication_id, legacy_id = publication_snapshot_ids(selected)
        # Short names fit ordinary Windows checkout/test roots. A saved old
        # receipt or immutable directory still owns its previous identity;
        # changing formats must not publish a second batch after an upgrade.
        if (root / "releases" / legacy_id).is_dir() or db.execute("SELECT 1 FROM releases WHERE version=?", (legacy_id,)).fetchone():
            publication_id = legacy_id
        existing = db.execute("SELECT document FROM releases WHERE version=?", (publication_id,)).fetchone()
        if existing is not None and all(item.get("publication_status") == "published"
                                        and item.get("publication_version") == publication_id for item in selected):
            return {**json.loads(existing[0]), "replayed": True}
        result = publish_assets(root, selected, actor, publication_id=publication_id)
        db.execute("INSERT OR IGNORE INTO releases VALUES(?,?,?)", (result["version"], encode(result), stamp()))
        for item in selected:
            item.pop("image_path", None)
            item["publication_status"], item["publication_version"] = "published", result["version"]
            require(db.execute("UPDATE assets SET document=? WHERE id=? AND revision=?",
                               (encode(item), item["id"], item["revision"])).rowcount == 1, "发布时素材版本已变化")
        store.audit(db, result["version"], actor, "publish", {"assets": asset_ids,
                    "receipt_status": "unconfirmed", "review_method": "automated" if automated_only else "explicit"})
        return result


def withdraw_assets(root: Path, assets: list[dict], actor: str, reason: str) -> dict:
    """Permanently revoke published case IDs using the existing signed protocol.

    Signing finishes before a caller changes local editorial state. There is no
    distributed transaction or consumer receipt; callers must retain the signed
    withdrawal as authoritative if a later local database commit is interrupted.
    """
    root = Path(root)
    _text(actor, "authenticated publisher")
    _require(isinstance(reason, str) and 0 < len(reason.strip()) <= 2000,
             "请填写不超过 2000 字的永久撤回原因")
    _require(isinstance(assets, list) and 0 < len(assets) <= 50, "请明确选择 1–50 张已发布素材")
    ids = [_identifier(asset.get("id")) for asset in assets if isinstance(asset, dict)]
    _require(len(ids) == len(assets) and len(set(ids)) == len(ids), "撤回素材无效或重复")
    key_file = os.environ.get("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE", "").strip()
    _require(bool(key_file), "signing key is not configured")
    try:
        private_key = load_private_key(key_file, root)
    except (OSError, ValueError, TypeError) as exc:
        raise KnowledgeError("signing key configuration is invalid") from exc
    public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = os.environ.get("OMNISIGNAL_KNOWLEDGE_KEY_ID") or "ed25519-" + digest(public)[:16]
    _identifier(key_id)
    with _lock(root / ".workbench"):
        history = _history(root)
        _require(all(asset_id in history for asset_id in ids), "选中素材没有可撤回的签名发布记录")
        previous = [int(path.stem) for path in (root / "withdrawals").glob("*.json")]
        sequence = max(previous, default=0) + 1
        signed = publish_withdrawals(root=root, sequence=sequence, case_ids=ids,
                                     private_key=private_key, key_id=key_id)
        sha = digest((root / "withdrawals" / f"{sequence}.json").read_bytes())
    return {"version": f"withdrawal-{sequence}", "sequence": sequence, "status": "withdrawn",
            "receipt_status": "unconfirmed", "permanent": True, "asset_ids": ids,
            "assets_count": len(ids), "withdrawal_sha256": sha, "key_id": key_id,
            "cumulative_count": len(signed["case_ids"]), "reason": reason.strip()}
