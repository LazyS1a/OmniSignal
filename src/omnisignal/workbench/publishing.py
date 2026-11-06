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
    KnowledgeError, MAX_IMAGE_BYTES, MAX_PACKAGE_BYTES, MAX_RECORDS, MAX_RECORD_BYTES, _file, _identifier, _image, _lock,
    _require, _resolved, _text, canonical, digest, load_private_key, make_record, publish_release,
    read_latest, _history, publish_withdrawals, verify_release, materialize_release, _withdrawn, release_capacity,
)
from .review import DESIGN_FIELDS, fresh_review
from .taxonomy import fresh_taxonomy
from .store import Conflict, asset_from_row, encode, require, stamp
from omnisignal.knowledge.taxonomy import TAXONOMY_VERSION
from .publication_use import delivery_projection, validate_policy, library_recipient, target_delivered, ensure_table
from .library_publication import base_root, target_root, require_library_target, publication_targets
from omnisignal.knowledge.scope import recipient_key, envelope_recipient
from omnisignal.knowledge import snapshot_v3
PURPOSES = {"brand", "conversion"}
CHECKPOINT_CHAIN_LIMIT = 16
PUBLICATION_BUDGET_ENV = "OMNISIGNAL_WORKBENCH_PUBLICATION_MAX_PACKAGE_MIB"
RECEIVER_DEFAULT_PACKAGE_MIB = 128
RECEIVER_RECORD_FILE_BYTES = 128 * 1024 * 1024
CAPACITY_FAILURES = frozenset({
    "publication checkpoint exceeds record capacity", "publication checkpoint exceeds image budget",
    "publication image budget exceeded", "package size limit: publication byte budget exceeded",
    "publication records file exceeds receiver capacity", "publication manifest exceeds size limit",
    "record too large", "oversized record",
    "snapshot record count limit", "snapshot part count limit", "snapshot partition depth exhausted",
    "snapshot manifest size limit", "snapshot image capacity exceeded",
    "snapshot retained capacity exceeded", "snapshot disk capacity unavailable",
})


def current_publication_latest(root):
    """A closed feature switch never erases signed history or receipt debt."""
    legacy, snapshot = read_latest(root), snapshot_v3.read_snapshot_latest(root)
    _require(legacy is None or snapshot is None or legacy["sequence"] != snapshot["sequence"],
             "publication sequence conflicts across protocols")
    return max((pointer for pointer in (legacy, snapshot) if pointer is not None),
               key=lambda pointer: pointer["sequence"], default=None)


def current_publication_cases(root):
    snapshot = snapshot_v3.read_snapshot_latest(root)
    if snapshot is not None and current_publication_latest(root) == snapshot:
        return {key: value for key, value in snapshot_v3.snapshot_cases(root).items() if key not in _withdrawn(Path(root))}
    return materialize_release(root)


def publication_signed_exists(root, release_id):
    return any(path.exists() or path.is_symlink() for path in (
        Path(root) / "releases" / release_id, snapshot_v3.storage(root) / "releases" / release_id))


def _all_published_ids(root, recipient=None, trusted_keys=None):
    ids = set(_history(Path(root)))
    directory = _file(snapshot_v3.storage(root), "releases")
    if directory.exists():
        for entry in directory.iterdir():
            if entry.is_dir():
                manifest = snapshot_v3.read_manifest(root, entry.name, trusted_keys)
                _require(manifest["recipient"] == recipient, "snapshot withdrawal recipient mismatch")
                records, _ = snapshot_v3.snapshot_inventory(root, manifest)
                ids.update(record["case_id"] for record in records)
    return ids


def _publish_snapshot_assets(root, records, stage, private_key, key_id, recipient, publication_id, before_sign):
    _require(recipient is not None, "snapshot v3 requires a scoped library recipient")
    requested = records
    release_id = publication_id or "ad-snapshot-" + uuid.uuid4().hex[:20]
    known = _file(snapshot_v3.storage(root), "releases/" + release_id + "/manifest.json")
    sources = {image["path"]: _file(stage, image["path"]) for record in records for image in record["case"]["images"]}
    if known.exists():
        prior = snapshot_v3.read_manifest(root, release_id, {key_id: private_key.public_key()})
        _require(prior["recipient"] == recipient and prior["request_hash"] == snapshot_v3.request_hash(requested, recipient),
                 "snapshot recovery content conflict")
        records, _ = snapshot_v3.snapshot_inventory(root, prior, verify_images=True)
    else:
        pointer = snapshot_v3.read_snapshot_latest(root)
        if pointer is not None:
            prior = snapshot_v3.read_manifest(root, pointer["release_id"], {key_id: private_key.public_key()})
            _require(prior["recipient"] == recipient, "snapshot recipient mismatch")
            historical, _ = snapshot_v3.snapshot_inventory(root, prior)
            merged = {record["case_id"]: record for record in historical}
        else:
            merged = {}
            for manifest, historical, directory in reversed(_verified_chain(root, key_id, private_key)):
                _require(envelope_recipient(manifest) == recipient, "snapshot legacy recipient mismatch")
                for record in historical:
                    if record["op"] == "delete":
                        merged.pop(record["case_id"], None)
                    else:
                        merged[record["case_id"]] = record
                        for image in record["case"]["images"]:
                            sources.setdefault(image["path"], _file(directory, image["path"]))
        merged.update({record["case_id"]: record for record in records})
        withdrawn = _withdrawn(root)
        records = [merged[key] for key in sorted(merged) if key not in withdrawn]
    manifest = snapshot_v3.publish_snapshot(root=root, release_id=release_id, records=records,
        requested_records=requested, asset_sources=sources, private_key=private_key, key_id=key_id,
        recipient=recipient, taxonomy_version=TAXONOMY_VERSION, before_sign=before_sign)
    path = _file(snapshot_v3.storage(root), "releases/" + release_id + "/manifest.json")
    return {"version": release_id, "release_id": release_id, "sequence": manifest["sequence"], "recipient": recipient,
            "schema_version": 3, "status": "published", "receipt_status": "unconfirmed", "manifest_sha256": digest(path.read_bytes()),
            "assets_count": manifest["records_count"], "requested_assets_count": len(requested), "kind": "snapshot",
            "key_id": key_id, "taxonomy_version": TAXONOMY_VERSION, "parts_count": len(manifest["parts"]),
            "image_count": manifest["image_count"], "image_bytes": manifest["image_bytes"]}


def publication_package_budget():
    """Match B's default, or an explicit operator-confirmed receiver limit."""
    raw = os.environ.get(PUBLICATION_BUDGET_ENV, "").strip()
    _require(not raw or raw.isascii() and raw.isdecimal() and not raw.startswith("0")
             and len(raw) <= 3 and 1 <= int(raw) <= 512, "invalid receiver package byte budget")
    return (int(raw) if raw else RECEIVER_DEFAULT_PACKAGE_MIB) * 1024 * 1024


def select_capacity_batch(store, batch):
    """Unsigned candidates only. Reserve maximum legal JSONL per record.

    No permissions, signatures or membership are persisted here. Publication
    revalidates the frozen snapshot and exact final bytes, including checkpoints.
    A caller must never use this to split an already frozen/signed recovery batch.
    """
    limit = publication_package_budget()
    selected, images, image_bytes, record_bytes = [], {}, 0, 0
    for item in batch:
        asset = store.asset(item["id"])
        _require(asset["revision"] == item["revision"] and asset["sha256"] == item["sha256"],
                 "publication capacity snapshot changed")
        source = _file(store.images, asset["image_file"])
        _require(source.is_file() and 0 < source.stat().st_size <= MAX_IMAGE_BYTES, "missing/oversized image")
        size = source.stat().st_size
        previous_size = images.get(item["sha256"])
        _require(previous_size is None or previous_size == size, "publication image size conflict")
        extra = size if previous_size is None else 0
        reserved = record_bytes + MAX_RECORD_BYTES + 1
        if len(selected) >= MAX_RECORDS or reserved > RECEIVER_RECORD_FILE_BYTES or reserved + image_bytes + extra > limit:
            continue
        selected.append(item)
        images[item["sha256"]] = size
        image_bytes += extra
        record_bytes = reserved
    return selected, {"status": "bounded", "max_package_bytes": limit,
                      "limit_source": "explicit" if os.environ.get(PUBLICATION_BUDGET_ENV, "").strip() else "receiver_default_128_mib",
                      "selected": len(selected), "deferred": len(batch) - len(selected),
                      "reserved_record_bytes": record_bytes, "image_bytes": image_bytes,
                      "checkpoint_included": False}


def publication_snapshot_ids(items, recipient=None):
    """Current and legacy stable IDs for the same frozen asset snapshots."""
    identity = [[item["id"], item["revision"], item["sha256"]] for item in sorted(items, key=lambda item: item["id"])]
    if recipient is not None:
        identity = {"recipient": recipient, "assets": identity}
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
    refs, sources = set(), {}
    size = 0
    for record in records:
        length = len(canonical(record))
        _require(length <= MAX_RECORD_BYTES, "oversized record")
        size += length + 1
    _require(size <= RECEIVER_RECORD_FILE_BYTES, "publication records file exceeds receiver capacity")
    for record in records:
        if record["op"] != "upsert":
            continue
        for entry in record["case"]["images"]:
            name = entry["path"]
            if name not in refs:
                source = _file(locations[record["case_id"]], name)
                _require(source.is_file() and 0 < source.stat().st_size <= MAX_IMAGE_BYTES, "missing/oversized image")
                size += source.stat().st_size
                sources[name] = source
            refs.add(name)
            _require(size <= publication_package_budget(), "publication checkpoint exceeds image budget")
    # Budget the merged checkpoint before copying its historical image set.
    for name, source in sources.items():
        target = _file(stage, name)
        if not target.exists():
            data = source.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    # Omitted/withdrawn earlier records must not leave unreferenced staged files.
    for path in (stage / "images").glob("*"):
        if path.relative_to(stage).as_posix() not in refs:
            path.unlink()
    _require(sum((stage / name).stat().st_size for name in refs) <= publication_package_budget(),
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
    if asset.get("publication_use_basis") is not None:
        case["editorial"]["publication_use_basis"] = asset["publication_use_basis"]
    return case


def publish_assets(root: Path, assets: list[dict], actor: str, *, publication_id=None, before_sign=None, recipient=None) -> dict:
    """Publish all supplied assets or fail; retain prior releases via deltas.

    ``root`` is the publication root. The signing key must be supplied separately
    using OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE. An optional stable key ID is read
    from OMNISIGNAL_KNOWLEDGE_KEY_ID; otherwise a public-key fingerprint is used.
    B must trust that public key out of band. Existing ``nova-1`` vocabulary and
    schema 1 stay unchanged; advertising purposes are editorial metadata.
    """
    root = Path(root)
    _require(snapshot_v3.enabled() or snapshot_v3.read_snapshot_latest(root) is None,
             "snapshot v3 publication disabled; legacy downgrade forbidden")
    if publication_id is not None:
        _identifier(publication_id)
    _text(actor, "authenticated publisher")
    _require(isinstance(assets, list) and 0 < len(assets) <= MAX_RECORDS,
             "select between 1 and 2000 accepted assets")
    _require(all(isinstance(asset, dict) for asset in assets), "invalid asset")
    asset_ids = [_identifier(asset.get("id")) for asset in assets]
    _require(len(set(asset_ids)) == len(asset_ids), "duplicate asset in publication")
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
    # A full checkpoint can be much larger than the container's small /tmp
    # tmpfs. Stage on the existing knowledge volume, outside its HTTP release
    # namespace. Each invocation owns and cleans only its unique directory.
    stage_parent = _file(root, ".workbench/staging")
    stage_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _file(root, ".workbench/staging")  # Recheck after creating parent components.
    with tempfile.TemporaryDirectory(prefix="omni-publish-", dir=stage_parent) as directory:
        stage = Path(directory)
        _require(not stage.is_symlink() and _resolved(stage).is_relative_to(_resolved(stage_parent)),
                 "publication staging escaped its data volume")
        records, image_bytes, image_paths = [], 0, set()
        for asset in assets:
            record = make_record(_prepare_asset(asset, stage))
            records.append(record)
            path = record["case"]["images"][0]["path"]
            if path not in image_paths:
                image_bytes += (stage / path).stat().st_size
                image_paths.add(path)
            _require(image_bytes <= (snapshot_v3.image_budget() if snapshot_v3.enabled() else publication_package_budget()), "publication image budget exceeded")
        _require(len({record["case_id"] for record in records}) == len(records),
                 "duplicate asset in publication")
        with _lock(root / ".workbench"):
            if snapshot_v3.enabled():
                return _publish_snapshot_assets(root, records, stage, private_key, key_id, recipient, publication_id, before_sign)
            latest = read_latest(root)
            sequence = 1 if latest is None else latest["sequence"] + 1
            release_id = publication_id or f"ad-{sequence}-{uuid.uuid4().hex[:12]}"
            kind, parent = "full" if latest is None else "delta", None if latest is None else latest["release_id"]
            requested_count = len(records)
            if publication_id is not None and (root / "releases" / release_id).exists():
                prior, prior_records = verify_release(root / "releases" / release_id, {key_id: private_key.public_key()})
                _require(envelope_recipient(prior) == recipient, "publication recovery recipient mismatch")
                prior_by_id = {record["case_id"]: record for record in prior_records}
                _require(all(prior_by_id.get(record["case_id"]) == record for record in records), "publication recovery content conflict")
                _require(prior["kind"] == "full" or prior_records == records, "publication recovery content conflict")
                records = prior_records
                _stage_records(records, {record["case_id"]: root / "releases" / release_id for record in records}, stage)
                sequence, kind, parent = prior["sequence"], prior["kind"], prior["parent_release_id"]
            elif latest is not None:
                chain = _verified_chain(root, key_id, private_key)
                _require(all(envelope_recipient(manifest) == recipient for manifest, _, _ in chain), "publication chain recipient mismatch")
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
            # Preparing/copying image bytes can take time. Re-read operator
            # policy inside the publication lock, after preparation and directly
            # before creating any new signed files.
            # A full checkpoint includes historical records/images, not only
            # this tick's selected batch. Never sign an unreceivable final set.
            capacity = release_capacity(records, stage, max_package_bytes=publication_package_budget())
            _require(capacity["record_bytes"] <= RECEIVER_RECORD_FILE_BYTES,
                     "publication records file exceeds receiver capacity")
            if before_sign is not None:
                before_sign()
            publish_release(
                root=root, release_id=release_id, sequence=sequence, records=records,
                asset_root=stage, private_key=private_key, key_id=key_id,
                kind=kind, parent_release_id=parent,
                taxonomy_version=TAXONOMY_VERSION,
                recipient=recipient,
                max_package_bytes=capacity["max_package_bytes"],
            )
            manifest_sha256 = digest((root / "releases" / release_id / "manifest.json").read_bytes())
    return {"version": release_id, "release_id": release_id, "sequence": sequence,
            **({"recipient": recipient} if recipient is not None else {}),
            "status": "published", "receipt_status": "unconfirmed",
            "manifest_sha256": manifest_sha256, "assets_count": len(records),
            "requested_assets_count": requested_count, "kind": kind,
            "key_id": key_id, "taxonomy_version": TAXONOMY_VERSION}


def publish_store_assets(store, asset_ids, actor, *, expected_revisions=None,
                         automated_only=False, publication_root=None, publication_policy=None, policy_loader=None):
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
    require(publication_policy is None or automated_only, "internal_publication_requires_automated_review")
    publication_policy = validate_policy(publication_policy)
    recipient = library_recipient(publication_policy)
    root = target_root(store, publication_policy, publication_root)
    def check_policy():
        if policy_loader is not None:
            current = policy_loader()
            require(current and current["enabled"] and current["auto_publish"]
                    and current.get("publication_authorized") == publication_policy, "publication_policy_changed")
        if recipient is not None:
            require_library_target(root, publication_policy)
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if recipient is not None:
            ensure_table(db)
        check_policy()
        globally_withdrawn = set().union(*(_withdrawn(directory) for directory, _ in publication_targets(base_root(store, publication_root))))
        require(not (set(asset_ids) & globally_withdrawn), "素材已有永久撤回签名，不能重新发布")
        # A checkpoint carries the already published inventory forward. Refuse
        # to carry cases whose current source record lost its use basis.
        for case_id, signed_case in current_publication_cases(root).items():
            require(case_id not in globally_withdrawn, "存在尚未完成的跨目标撤回，暂停发布")
            prior_row = db.execute("SELECT * FROM assets WHERE id=?", (case_id,)).fetchone()
            require(prior_row is not None, "已发布素材的本地用途记录缺失，暂停发布")
            prior_asset = asset_from_row(prior_row, strict=True)
            require(prior_asset.get("status") == "accepted" and prior_asset.get("publication_status") != "withdrawn",
                    "已发布素材用途状态已变化，需先处理撤回")
            recovering = (case_id in asset_ids and automated_only and publication_policy is not None
                          and (not target_delivered(db, prior_asset, recipient) if recipient is not None
                               else prior_asset.get("publication_status") != "published"))
            if recovering:
                from .review import validate_automated_quality
                validate_automated_quality(db, prior_asset)
            prior_projected = delivery_projection(store, db, prior_asset, policy=publication_policy if recovering else None,
                                                 create=recovering, recipient=recipient)
            signed_basis = signed_case.get("editorial", {}).get("publication_use_basis")
            require(signed_basis == prior_projected.get("publication_use_basis"), "已发布素材用途依据已变化，需先处理撤回")
        selected = []
        originals = {}
        for asset_id in sorted(asset_ids):
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            require(row is not None, "选中的素材不存在")
            asset = asset_from_row(row, strict=True)
            if expected_revisions is not None:
                require(expected_revisions.get(asset_id) == asset["revision"], "素材已被修改，请重新确认发布")
            if automated_only:
                reviewed, taxonomy = fresh_review(asset), fresh_taxonomy(asset)
                require(reviewed is not None and reviewed["provenance"] == "model_automated_review"
                        and taxonomy is not None and taxonomy.get("review_method") == "automated", "自动发布需要当前版本的 AI 分析和分类复核")
                require(asset.get("publication_status") != "withdrawn", "素材已永久撤回")
            # Clicking explicit publish must not bypass the same v2 AI evidence
            # guard used by the autonomous worker. Human review remains explicit.
            if asset.get("reviewed_analysis", {}).get("provenance") == "model_automated_review":
                from .review import validate_automated_quality
                validate_automated_quality(db, asset)
            originals[asset_id] = asset
            asset = delivery_projection(store, db, asset, policy=publication_policy,
                                        create=automated_only and publication_policy is not None)
            asset["image_path"] = str(store.images / asset["image_file"])
            selected.append(asset)
        publication_id, legacy_id = publication_snapshot_ids(selected, recipient)
        # Short names fit ordinary Windows checkout/test roots. A saved old
        # receipt or immutable directory still owns its previous identity;
        # changing formats must not publish a second batch after an upgrade.
        if publication_signed_exists(root, legacy_id) or db.execute("SELECT 1 FROM releases WHERE version=?", (legacy_id,)).fetchone():
            publication_id = legacy_id
        existing = db.execute("SELECT document FROM releases WHERE version=?", (publication_id,)).fetchone()
        if existing is not None and all(target_delivered(db, item, recipient) if recipient is not None else
                                        item.get("publication_status") == "published" and item.get("publication_version") == publication_id for item in selected):
            return {**json.loads(existing[0]), "replayed": True}
        check_policy()
        result = publish_assets(root, selected, actor, publication_id=publication_id, before_sign=check_policy, recipient=recipient)
        db.execute("INSERT OR IGNORE INTO releases VALUES(?,?,?)", (result["version"], encode(result), stamp()))
        for item in originals.values():
            if recipient is not None:
                db.execute("INSERT OR IGNORE INTO publication_target_deliveries VALUES(?,?,?,?)",
                           (item["id"], item["revision"], recipient_key(recipient), result["version"]))
                continue
            item["publication_status"], item["publication_version"] = "published", result["version"]
            require(db.execute("UPDATE assets SET document=? WHERE id=? AND revision=?",
                               (encode(item), item["id"], item["revision"])).rowcount == 1, "发布时素材版本已变化")
        store.audit(db, result["version"], actor, "publish", {"assets": asset_ids,
                    **({"recipient": recipient} if recipient is not None else {}),
                    "receipt_status": "unconfirmed", "review_method": "automated" if automated_only else "explicit"})
        return result


def withdraw_assets(root: Path, assets: list[dict], actor: str, reason: str, *, recipient=None) -> dict:
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
        history = _all_published_ids(root, recipient, {key_id: private_key.public_key()})
        _require(all(asset_id in history for asset_id in ids), "选中素材没有可撤回的签名发布记录")
        previous = [int(path.stem) for path in (root / "withdrawals").glob("*.json")]
        sequence = max(previous, default=0) + 1
        signed = publish_withdrawals(root=root, sequence=sequence, case_ids=ids,
                                     private_key=private_key, key_id=key_id, recipient=recipient)
        sha = digest((root / "withdrawals" / f"{sequence}.json").read_bytes())
    return {"version": f"withdrawal-{sequence}" if recipient is None else f"withdrawal-{recipient_key(recipient)[:16]}-{sequence}",
            **({"recipient": recipient} if recipient is not None else {}), "sequence": sequence, "status": "withdrawn",
            "receipt_status": "unconfirmed", "permanent": True, "asset_ids": ids,
            "assets_count": len(ids), "withdrawal_sha256": sha, "key_id": key_id,
            "cumulative_count": len(signed["case_ids"]), "reason": reason.strip()}


def withdraw_publication_targets(root, assets, actor, reason):
    """An explicit source withdrawal reaches every previously delivered target.

    Completed signatures stay authoritative if a later target fails. A caller
    receives an error, never a success for a partial withdrawal; retrying this
    explicit action is safe and does not involve analysis/model jobs.
    """
    from .library_publication import publication_targets
    targets, covered = [], set()
    for directory, recipient in publication_targets(root):
        history = _all_published_ids(directory, recipient)
        selected = [asset for asset in assets if asset["id"] in history]
        if selected:
            targets.append((directory, recipient, selected))
            covered.update(asset["id"] for asset in selected)
    require(covered == {asset["id"] for asset in assets}, "选中素材没有可撤回的签名发布记录")
    results = [withdraw_assets(directory, selected, actor, reason, recipient=recipient)
               for directory, recipient, selected in targets]
    if len(results) == 1:
        return results[0]
    return {"version": "withdrawal-set-" + hashlib.sha256(encode([result["version"] for result in results]).encode()).hexdigest()[:24],
            "status": "withdrawn", "receipt_status": "unconfirmed", "permanent": True,
            "asset_ids": sorted(covered), "assets_count": len(covered), "targets": results, "reason": reason.strip()}
