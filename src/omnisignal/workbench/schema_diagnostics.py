"""Allowlisted schema failure metadata, never model values or exception details."""
from __future__ import annotations

import json

# These strings are application-owned validator messages. Unknown messages and
# dynamic KeyError/TypeError text must never cross into the stored diagnostic.
_MESSAGES = {
    "模型返回字段不完整或包含不支持字段": ("result_fields", "result"),
    "模型返回缺少逐图分类": ("taxonomy_missing", "result.taxonomy"),
    "模型返回缺少八项设计分析": ("design_missing", "result.design"),
    "模型返回的设计分析字段无效": ("design_fields", "result.design"),
    "模型返回的广告分类无效": ("purposes_invalid", "result.purposes"),
    "模型返回的素材类型无效": ("kind_invalid", "result.kind"),
    "模型返回文字无效或过长": ("result_text_invalid", "result"),
    "模型返回缺少判断依据": ("reason_missing", "result.reason"),
    "广告建议缺少传播目的": ("advertisement_purpose_missing", "result.purposes"),
    "模型分类必须且仅包含 tags 和 note": ("taxonomy_fields", "result.taxonomy"),
    "模型分类分组无效": ("taxonomy_groups", "result.taxonomy.tags"),
    "模型分类不在当前字典中或包含重复标签": ("taxonomy_labels", "result.taxonomy.tags"),
    "模型分类需要逐图依据": ("taxonomy_evidence", "result.taxonomy.note"),
    "专项分析响应必须为对象": ("envelope_type", "response"),
    "专项分析响应缺少结果或详情": ("envelope_fields", "response"),
    "专项分析结果字段缺失或包含额外字段": ("result_fields", "result"),
    "专项分析详情必须为对象": ("details_type", "details"),
    "专项分析详情字段无效": ("details_fields", "details"),
    "专项分析规则无效": ("profile_invalid", "details.profile"),
    "专项分析文字无效": ("details_text_invalid", "details"),
    "专项分析列表无效": ("details_list_invalid", "details"),
    "专项分析需五项图像观察": ("observations_count", "details.observations"),
    "图像观察字段无效": ("observation_fields", "details.observations[]"),
    "精简图像观察字段无效": ("compact_observation_fields", "details.observations[]"),
    "图像观察标识无效": ("observation_id", "details.observations[].id"),
    "图像观察维度无效": ("observation_field", "details.observations[].field"),
    "观察与旧协议投影不一致": ("observation_projection", "details.observations[].text"),
    "来源陈述字段无效": ("source_statement_fields", "details.source_statements[]"),
    "来源引文不在当前来源资料内": ("source_quote_unbound", "details.source_statements[].source_quote"),
    "专项分析需三项分析推断": ("inferences_count", "details.inferences"),
    "分析推断字段无效": ("inference_fields", "details.inferences[]"),
    "精简分析推断字段无效": ("compact_inference_fields", "details.inferences[]"),
    "分析推断维度无效": ("inference_field", "details.inferences[].field"),
    "推断与旧协议投影不一致": ("inference_projection", "details.inferences[].text"),
    "专项分析引用缺失或指向不存在的观察": ("evidence_reference", "details.evidence_ids"),
    "灵感卡字段无效": ("card_fields", "details.inspiration_card"),
    "精简灵感卡字段无效": ("compact_card_fields", "details.inspiration_card"),
    "灵感卡与分析依据不一致": ("card_projection", "details.inspiration_card"),
    "内容自查字段无效": ("self_check_fields", "details.self_check[]"),
}
_FALLBACKS = {
    "response": ("response_shape_invalid", "response"),
    "result": ("result_validation_failed", "result"),
    "details": ("details_validation_failed", "details"),
}
_ALLOWED = set(_MESSAGES.values()) | set(_FALLBACKS.values())


def schema_diagnostic(error=None, *, stage="result"):
    # Only exact fixed application messages may supply a more specific code.
    pair = _MESSAGES.get(str(error)) if error is not None else None
    code, path = pair or _FALLBACKS.get(stage, _FALLBACKS["result"])
    return {"code": code, "path": path}


def safe_diagnostic(value):
    """Validate both writes and reads; legacy/unknown/malformed metadata is null."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return None
    if not isinstance(value, dict) or set(value) != {"code", "path"}:
        return None
    if not all(isinstance(value[k], str) for k in ("code", "path")):
        return None
    return dict(value) if (value["code"], value["path"]) in _ALLOWED else None
