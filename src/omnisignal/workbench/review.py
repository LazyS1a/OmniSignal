"""Bind explicitly reviewed design knowledge to one immutable image revision.

Model suggestions stay suggestions. Only an authenticated editorial review may
create this object; supplied provenance, model identity and freshness flags are
never accepted from a request payload.
"""
from __future__ import annotations

import hashlib
import json
import re

from .store import Conflict, encode, require, stamp


DESIGN_FIELDS = (
    "palette", "typography", "composition", "hierarchy", "imagery",
    "reusable_ideas", "applicability", "limitations",
)
EVIDENCE_KINDS = {"visual_observation", "analysis_inference"}
BOUND_FIELDS = {"schema_version", "asset_id", "revision", "sha256", "reviewed_by", "reviewed_at",
                "model_version", "provenance", "source_job_id", "summary_zh", "design"}


def validated_content(payload):
    require(isinstance(payload, dict) and {"summary_zh", "design"} <= set(payload)
            <= {"source_job_id", "summary_zh", "design", "review_method"}, "审核分析字段无效；身份、版本与模型来源由服务端绑定")
    summary = payload["summary_zh"]
    require(isinstance(summary, str) and 0 < len(summary.strip()) <= 6000
            and re.search(r"[\u3400-\u9fff]", summary) is not None, "请填写不超过 6000 字的中文审核摘要")
    design = payload["design"]
    require(isinstance(design, dict) and set(design) == set(DESIGN_FIELDS), "设计分析必须完整包含八个字段")
    result = {}
    for field in DESIGN_FIELDS:
        value = design[field]
        require(isinstance(value, dict) and set(value) == {"text", "kind"}, "每项设计分析需要 text 和 kind")
        text, kind = value["text"], value["kind"]
        require(isinstance(text, str) and len(text) <= 1500, "单项设计分析应不超过 1500 字")
        require(isinstance(kind, str) and kind in EVIDENCE_KINDS, "设计依据只能选择图像观察或分析推断")
        result[field] = {"text": text.strip(), "kind": kind}
    require(any(value["text"] for value in result.values()), "请至少填写一项已核对的设计分析")
    job_id = payload.get("source_job_id")
    require(job_id is None or (isinstance(job_id, str) and 0 < len(job_id) <= 128), "分析任务来源无效")
    method = payload.get("review_method", "human")
    require(isinstance(method, str) and method in {"human", "assistant"}, "整理方式只能选择 human 或 assistant")
    require(method != "assistant" or job_id is None, "助手整理不能借用模型任务声称人工复核")
    return {"source_job_id": job_id, "review_method": method, "summary_zh": summary.strip(), "design": result}


def completed_source_job(db, asset, job_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_jobs'").fetchone():
        return None
    row = db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    if (job["asset_id"] != asset["id"] or job["revision"] != asset["revision"]
            or job["status"] != "completed"):
        return None
    expected_cache = hashlib.sha256(encode([asset["id"], asset["revision"], asset["sha256"], job["provider"]]).encode()).hexdigest()
    if job["cache_key"] != expected_cache:
        return None
    try:
        suggestion = json.loads(job["suggestion"])
    except (TypeError, ValueError):
        return None
    if not isinstance(suggestion, dict) or suggestion.get("source_image_id") != asset["id"]:
        return None
    model = suggestion.get("model_version")
    if not isinstance(model, str) or not 0 < len(model.strip()) <= 200 or model == "none":
        return None
    return {**job, "suggestion": suggestion, "model_version": model}


def bind_reviewed_analysis(db, asset, payload, actor, new_revision):
    """Call inside the same BEGIN IMMEDIATE transaction as the review CAS."""
    content = validated_content(payload)
    method = content.pop("review_method")
    require(isinstance(actor, str) and 0 < len(actor.strip()) <= 1000, "缺少已认证的审核人")
    model = "none"
    provenance = "assistant_editorial_review" if method == "assistant" else "manual_human_review"
    if content["source_job_id"] is not None:
        job = completed_source_job(db, asset, content["source_job_id"])
        require(job is not None, "分析任务必须属于当前素材、当前版本和图片，且已真实完成；请重新分析或改为独立人工审核")
        model, provenance = job["model_version"], "model_assisted_human_review"
    return {**content, "schema_version": 1, "asset_id": asset["id"], "revision": new_revision,
            "sha256": asset["sha256"], "reviewed_by": actor, "reviewed_at": stamp(),
            "model_version": model, "provenance": provenance}


def fresh_review(asset):
    """Return valid fresh knowledge, or None only when no review has ever existed."""
    if "reviewed_analysis" not in asset:
        return None
    review = asset["reviewed_analysis"]
    automated = isinstance(review, dict) and review.get("schema_version") == 2
    require(isinstance(review, dict) and set(review) == (BOUND_FIELDS | {"review_method"} if automated else BOUND_FIELDS), "已保存的审核分析格式无效，请重新审核")
    require(review["schema_version"] in {1, 2} and type(review["revision"]) is int
            and review["asset_id"] == asset["id"] and review["revision"] == asset["revision"]
            and review["sha256"] == asset["sha256"], "审核分析已过期，发布前请对当前素材版本重新审核")
    validated_content({key: review[key] for key in ("source_job_id", "summary_zh", "design")})
    require(isinstance(review["reviewed_by"], str) and bool(review["reviewed_by"].strip())
            and isinstance(review["reviewed_at"], str) and bool(review["reviewed_at"]), "审核来源信息不完整")
    if automated:
        require(review["review_method"] == "automated" and review["reviewed_by"] == "system:auto-editorial"
                and review["provenance"] == "model_automated_review" and isinstance(review["source_job_id"], str)
                and bool(review["source_job_id"]) and isinstance(review["model_version"], str)
                and 0 < len(review["model_version"]) <= 200 and review["model_version"] != "none", "自动审核来源无效")
    elif review["source_job_id"] is None:
        require(review["model_version"] == "none" and review["provenance"] in {"manual_human_review", "assistant_editorial_review"}, "独立整理不能伪称模型任务来源")
    else:
        require(isinstance(review["model_version"], str) and 0 < len(review["model_version"]) <= 200
                and review["model_version"] != "none" and review["provenance"] == "model_assisted_human_review", "模型辅助审核来源无效")
    return review


def automated_candidate(db, asset, job_id):
    """Conservative acceptance gate, not a claim of human or quality certification."""
    from .taxonomy import validated_taxonomy
    require(asset.get("status") == "pending" and not asset.get("reviewed_analysis")
            and not asset.get("reviewed_taxonomy"), "human_review_required")
    require(asset.get("permissions", {}).get("analysis") == "allowed", "analysis_permission_required")
    job = completed_source_job(db, asset, job_id)
    require(job is not None and job["charged"] == 1, "stale_or_uncompleted_job")
    require(asset.get("analysis_job_id") == job_id and asset.get("analysis_suggestion_revision") == asset["revision"]
            and asset.get("analysis_suggestion") == job["suggestion"], "stale_suggestion")
    suggestion = job["suggestion"]
    require(job["model_version"].lower().startswith("qwen"), "qwen_model_required")
    require(suggestion.get("kind") in {"advertisement", "reference"}, "uncertain_kind")
    from .analysis import _result
    clean = {key: suggestion[key] for key in ("purposes", "kind", "reason", "description", "ocr_text", "brand", "design", "taxonomy") if key in suggestion}
    _result(clean, asset["id"], require_taxonomy=True)
    taxonomy = validated_taxonomy(clean["taxonomy"])
    require(len(taxonomy["note"]) >= 10 and re.search(r"图|画面|可见", taxonomy["note"]) is not None,
            "taxonomy_visual_evidence_required")
    require(not any(word in clean["reason"] + taxonomy["note"] for word in ("无法判断", "无法确定", "不确定", "待确认", "可能是")), "uncertain_evidence")
    require(len(clean["description"].strip()) >= 12 and re.search(r"[\u3400-\u9fff]", clean["description"])
            and len(clean["reason"].strip()) >= 10, "insufficient_summary")
    placeholders = {"图中未体现", "无法判断", "未知", "不适用", "无", "none", "n/a"}
    require(all(len(text.strip()) >= 4 for text in clean["design"].values()), "insufficient_design")
    require(all(len(clean["design"][field].strip()) >= 10 and clean["design"][field].strip() not in placeholders
                for field in ("palette", "composition", "hierarchy", "imagery", "reusable_ideas", "limitations")), "insufficient_design")
    return job, clean, taxonomy


def apply_automated_review(store, asset_id, expected_revision, job_id, actor="system:auto-editorial"):
    """Internal service entry; HTTP review payloads still cannot request automated review."""
    from .taxonomy import bind_reviewed_taxonomy
    require(actor == "system:auto-editorial", "invalid_automated_actor")
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT document FROM assets WHERE id=?", (asset_id,)).fetchone()
        require(row is not None, "missing_asset")
        asset = json.loads(row[0])
        require(type(expected_revision) is int and asset["revision"] == expected_revision, "revision_changed")
        job, suggestion, taxonomy = automated_candidate(db, asset, job_id)
        path = store.images / asset["image_file"]
        require(path.parent.resolve() == store.images.resolve() and path.is_file() and not path.is_symlink()
                and path.stat().st_size <= 10 * 1024 * 1024
                and hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"], "image_changed")
        new_revision = asset["revision"] + 1
        reviewed = {"source_job_id": job_id, "summary_zh": suggestion["description"].strip(),
                    "design": {field: {"text": suggestion["design"][field].strip(),
                                       "kind": "visual_observation" if index < 5 else "analysis_inference"}
                               for index, field in enumerate(DESIGN_FIELDS)}}
        validated_content(reviewed)
        reviewed.update(schema_version=2, asset_id=asset_id, revision=new_revision, sha256=asset["sha256"],
                        reviewed_by=actor, reviewed_at=stamp(), model_version=job["model_version"],
                        provenance="model_automated_review", review_method="automated")
        classified = bind_reviewed_taxonomy(asset, taxonomy, actor, new_revision)
        classified.update(schema_version=2, review_method="automated", provenance="model_automated_review",
                          source_job_id=job_id, model_version=job["model_version"])
        asset.update(status="accepted", revision=new_revision, kind=suggestion["kind"], purposes=suggestion["purposes"],
                     reason=suggestion["reason"], reviewed_analysis=reviewed, reviewed_taxonomy=classified,
                     review_method="automated", analysis_status="analyzed")
        require(db.execute("UPDATE assets SET document=?,status='accepted',revision=? WHERE id=? AND revision=?",
                           (encode(asset), new_revision, asset_id, expected_revision)).rowcount == 1, "revision_changed")
        store.audit(db, asset_id, actor, "automated_review", {"revision": new_revision, "source_revision": expected_revision,
                    "source_job_id": job_id, "model_version": job["model_version"], "review_method": "automated",
                    "provenance": "model_automated_review", "tags": taxonomy["tags"]})
        return describe_review(asset, db)


def describe_review(asset, db=None):
    """Add derived UI status without persisting or trusting client freshness flags."""
    result = dict(asset)
    from .taxonomy import taxonomy_status
    result["reviewed_taxonomy_status"] = taxonomy_status(asset)
    try:
        reviewed = fresh_review(asset)
        result["reviewed_analysis_status"] = "current" if reviewed is not None else "none"
    except (Conflict, KeyError, TypeError):
        result["reviewed_analysis_status"] = "stale"
    result["analysis_suggestion_job_id"] = None
    job_id = asset.get("analysis_job_id")
    if db is not None and isinstance(job_id, str) and asset.get("analysis_suggestion_revision") == asset["revision"]:
        job = completed_source_job(db, asset, job_id)
        if job is not None and job["suggestion"] == asset.get("analysis_suggestion"):
            result["analysis_suggestion_job_id"] = job_id
    return result
