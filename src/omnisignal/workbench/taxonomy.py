"""Explicit human classification, bound to the current image and editorial revision."""
from __future__ import annotations

from omnisignal.knowledge.taxonomy import TAXONOMY, TAXONOMY_VERSION
from .store import Conflict, require, stamp

BOUND_FIELDS = {"schema_version", "taxonomy_version", "asset_id", "revision", "sha256",
                "reviewed_by", "reviewed_at", "tags", "note"}


def validated_taxonomy(payload):
    require(isinstance(payload, dict) and set(payload) == {"tags", "note"},
            "标签审核只接受 tags 和 note，审核身份与版本由服务端绑定")
    tags, note = payload["tags"], payload["note"]
    require(isinstance(tags, dict) and set(tags) == set(TAXONOMY), "分类必须包含行业、媒介和任务三个分组")
    result = {}
    for group, choices in TAXONOMY.items():
        values = tags[group]
        require(isinstance(values, list) and len(values) <= len(choices)
                and all(isinstance(value, str) and value in choices for value in values),
                "标签不在当前 nova-1 字典中")
        require(len(values) == len(set(values)), "分类标签不能重复")
        require(group == "task" or bool(values), "请明确核对至少一个行业和一个媒介；未知分类请保留待处理")
        result[group] = sorted(values)
    require(isinstance(note, str) and len(note) <= 2000, "分类依据应不超过 2000 字")
    require("other" not in result["industry"] or bool(note.strip()), "选择其他行业时请说明具体行业与分类依据")
    return {"tags": result, "note": note.strip()}


def bind_reviewed_taxonomy(asset, payload, actor, new_revision):
    content = validated_taxonomy(payload)
    require(isinstance(actor, str) and 0 < len(actor.strip()) <= 1000, "缺少已认证的审核人")
    return {**content, "schema_version": 1, "taxonomy_version": TAXONOMY_VERSION,
            "asset_id": asset["id"], "revision": new_revision, "sha256": asset["sha256"],
            "reviewed_by": actor, "reviewed_at": stamp()}


def fresh_taxonomy(asset):
    review = asset.get("reviewed_taxonomy")
    if review is None:
        return None
    require(isinstance(review, dict) and set(review) == BOUND_FIELDS,
            "已保存的分类审核格式无效，请重新核对标签")
    require(review["schema_version"] == 1 and review["taxonomy_version"] == TAXONOMY_VERSION
            and type(review["revision"]) is int and review["revision"] == asset["revision"]
            and review["asset_id"] == asset["id"] and review["sha256"] == asset["sha256"],
            "分类审核已过期，发布前请核对当前版本标签")
    validated_taxonomy({key: review[key] for key in ("tags", "note")})
    require(isinstance(review["reviewed_by"], str) and bool(review["reviewed_by"].strip())
            and isinstance(review["reviewed_at"], str) and bool(review["reviewed_at"]),
            "分类审核来源信息不完整")
    return review


def taxonomy_status(asset):
    try:
        return "current" if fresh_taxonomy(asset) is not None else "none"
    except (Conflict, KeyError, TypeError):
        return "stale"
