"""Publish reviewed assets through the existing schema-1 signed file protocol.

This adapter performs no network or model calls. Publication proves that a local
release was signed, never that an independently operated consumer received it.
Callers authenticate the actor and supply assets from their own editorial store.
"""
from __future__ import annotations

import io
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
    read_latest, _history, publish_withdrawals,
)
from .review import DESIGN_FIELDS, fresh_review
from .store import Conflict

TAXONOMY_VERSION = "nova-1"
PURPOSES = {"brand", "conversion"}


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
    except Conflict as exc:
        raise KnowledgeError(str(exc)) from None
    case = {
        "case_id": asset_id, "revision": revision, "title": asset["title"],
        "summary_zh": ("人工审核收录的设计参考：" if material_kind == "reference" else "人工审核收录的广告素材：") + asset["title"],
        "brand_or_team": brand, "review_status": "approved",
        "display_permission": "allowed", "permissions": permission_values,
        "analysis_version": "manual-ad-review-1", "model_version": "none",
        "tags": {"industry": ["other"], "medium": ["digital"]},
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
                      "legacy_medium_fallback": True,
                      "medium_note": "数字图片兼容分类；实际投放媒介尚未确认。", **notes},
    }
    if reviewed is not None:
        case["summary_zh"] = reviewed["summary_zh"]
        case["model_version"] = reviewed["model_version"]
        case["analysis_version"] = "human-design-review-1"
        case["claims"] = [
            {"claim_id": "design-" + field, "kind": reviewed["design"][field]["kind"],
             "text": reviewed["design"][field]["text"], "design_field": field,
             "source_ids": [], "image_ids": ["image-1"]}
            for field in DESIGN_FIELDS if reviewed["design"][field]["text"].strip()
        ]
        case["editorial"]["provenance"] = reviewed["provenance"]
        case["editorial"]["reviewed_analysis"] = reviewed
    return case


def publish_assets(root: Path, assets: list[dict], actor: str) -> dict:
    """Publish all supplied assets or fail; retain prior releases via deltas.

    ``root`` is the publication root. The signing key must be supplied separately
    using OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE. An optional stable key ID is read
    from OMNISIGNAL_KNOWLEDGE_KEY_ID; otherwise a public-key fingerprint is used.
    B must trust that public key out of band. Existing ``nova-1`` vocabulary and
    schema 1 stay unchanged; advertising purposes are editorial metadata.
    """
    root = Path(root)
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
            release_id = f"ad-{sequence}-{uuid.uuid4().hex[:12]}"
            publish_release(
                root=root, release_id=release_id, sequence=sequence, records=records,
                asset_root=stage, private_key=private_key, key_id=key_id,
                kind="full" if latest is None else "delta",
                parent_release_id=None if latest is None else latest["release_id"],
                taxonomy_version=TAXONOMY_VERSION,
            )
            manifest_sha256 = digest((root / "releases" / release_id / "manifest.json").read_bytes())
    return {"version": release_id, "release_id": release_id, "sequence": sequence,
            "status": "published", "receipt_status": "unconfirmed",
            "manifest_sha256": manifest_sha256, "assets_count": len(records),
            "key_id": key_id, "taxonomy_version": TAXONOMY_VERSION}


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
