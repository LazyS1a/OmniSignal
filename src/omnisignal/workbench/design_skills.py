"""Versioned, image-bound design analysis. Rules are our own adaptations, not tools.

The legacy eight-field projection remains the publication interchange format.
Evidence and content checks live in a separate job-bound record. A passed check
means structured AI evidence passed these rules; it is not a human review or a
grant of display/export rights, nor independent visual verification.
"""
from __future__ import annotations

import hashlib
import json
import re

from .store import Conflict, encode, require

SCHEMA_VERSION = 1
SKILL_VERSION = "omni-design-1"
PROMPT_VERSION = "omni-design-prompt-2"
VALIDATOR_VERSION = "omni-design-check-2"
PROVIDER_PREFIX = "design-v1:"
OBSERVATION_FIELDS = ("palette", "typography", "composition", "hierarchy", "imagery")
INFERENCE_FIELDS = ("reusable_ideas", "applicability", "limitations")
PROFILES = {
    "packaging-analysis": "包装：识别主要展示面、品牌与产品阅读顺序、识别锚点、可见系列变化；不能凭正面猜背面、结构和工艺。",
    "brand-visual-analysis": "品牌视觉：分析标志、字形、配色和图形各自角色及关系；单张图片不能证明全品牌系统一致。",
    "static-ad-analysis": "静态广告：分析沟通主张、主视觉、卖点证据、行动引导、信息密度；产品性能、认证、效果不能凭观感补造。",
    "general-analysis": "通用：混合、无法确定主媒介或不适用时使用；明确可见内容和未知，不强凑包装/广告分类。",
}
DETAIL_FIELDS = {"profile", "route_reason", "observations", "source_statements", "inferences", "unknowns", "inspiration_card", "self_check"}
CARD_FIELDS = {"task", "method", "applicability", "limitations", "evidence_ids"}

PROMPT = """
本任务启用专项设计分析。整个响应只能有result和details两个字段，形如
{"result":{"purposes":[],"kind":"reference","reason":"...","description":"...","ocr_text":"","brand":"","design":{八项设计字段},"taxonomy":{分类字段}},"details":{如下字段}}。
具体设计文字只在result.design写一次。不要返回权限或人工审核结论。
details 字段必须完整且仅包含：
profile：packaging-analysis / brand-visual-analysis / static-ad-analysis / general-analysis。
route_reason：依据本张图说明所选主媒介。主要是包装则优先包装，广告目的保留在result.purposes；
品牌系统应用或标识研究选品牌视觉；明确静态广告/海报选静态广告；冲突或未知选general-analysis。
不要按来源网站路由。包装对应taxonomy.medium包含packaging；品牌视觉包含identity；
静态广告包含poster或digital，且不包含packaging/identity。general允许无法确定。
专项规则：
""" + "\n".join(f"{key}: {rule}" for key, rule in PROFILES.items()) + """
observations：恰好五项，固定为[{"id":"o1","field":"palette"},{"id":"o2","field":"typography"},{"id":"o3","field":"composition"},{"id":"o4","field":"hierarchy"},{"id":"o5","field":"imagery"}]。不要加text；服务端会从result.design对应字段取得文字。该文字只描述本图可见内容。
source_statements：0至8项{text,source_quote}；source_quote必须逐字来自本次title/description中的连续片段，text仅概括该片段，不升级为已证实事实。
inferences：恰好三项{field,evidence_ids}，field分别为reusable_ideas、applicability、limitations；不要加text。evidence_ids引用支持该推断的observations.id，至少一项。不用来源陈述当图像证据。
unknowns：0至10条明确的未知项。缺失工艺或效果等局部信息放这里，不影响可见部分的分析。
inspiration_card：只含{task,evidence_ids}。task一句具体设计任务；evidence_ids列出支撑设计方法的观察id。不要重复method/applicability/limitations，服务端从result.design派生。
self_check：0至8项{field,evidence,action}，仅列需要处理的具体矛盾或无依据主张；没有问题用[]。
提交前对照图片检查：结果引用的是当前图吗？观察和推断对应吗？是否把材质、品牌、效果当事实？
品牌无法从图中文字辨认则result.brand为空；实际销量、点击率、印刷工艺等没有证据则保留未知。
每项精炼，不复制全部来源，不编置信分数。系统会检查跨字段一致、引用与来源引文。
"""


def versions():
    return {"schema_version": SCHEMA_VERSION, "skill_version": SKILL_VERSION,
            "prompt_version": PROMPT_VERSION, "validator_version": VALIDATOR_VERSION}


def system_prompt(legacy):
    """Remove the old root-object instruction instead of appending a conflict."""
    old = "返回且仅返回一个 JSON 对象，不得包裹代码块或附加说明。字段必须且仅为："
    require(old in legacy, "旧分析提示词结构已变化，需显式更新专项提示词")
    return legacy.replace(old, "返回一个 JSON 对象，根字段只能是 result 和 details。不得包裹代码块或附加说明。result 对象的字段必须且仅为：", 1) + PROMPT


def details_required(job):
    return str(dict(job).get("provider", "")).startswith(PROVIDER_PREFIX)


def details_digest(details):
    return hashlib.sha256(json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _text(value, limit=1500, empty=False):
    require(isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip())), "专项分析文字无效")
    return value.strip()


def _items(value, limit):
    require(isinstance(value, list) and len(value) <= limit, "专项分析列表无效")
    return value


def _evidence(value, ids):
    require(isinstance(value, list) and 0 < len(value) <= 5 and all(isinstance(x, str) and x in ids for x in value)
            and len(value) == len(set(value)), "专项分析引用缺失或指向不存在的观察")


def quality_checks(details, result):
    """Deterministic cross-checks, never a claim of independent visual accuracy."""
    issues = []
    def issue(code, field, evidence, action, severity="block"):
        issues.append({"code": code, "field": field, "severity": severity, "evidence": evidence,
                       "action": action, "message": action})
    medium = set(result.get("taxonomy", {}).get("tags", {}).get("medium", []))
    profile = details["profile"]
    route_ok = (profile == "general-analysis" or
                profile == "packaging-analysis" and "packaging" in medium or
                profile == "brand-visual-analysis" and "identity" in medium and "packaging" not in medium or
                profile == "static-ad-analysis" and bool(medium & {"poster", "digital"}) and not medium & {"packaging", "identity"})
    if not route_ok:
        issue("profile_taxonomy_conflict", "profile", details["route_reason"], "核对主媒介与所选分析规则")
    if result["kind"] == "uncertain" or profile == "general-analysis":
        issue("classification_uncertain", "profile", details["route_reason"], "补足逐图分类依据后再自动收录")
    texts = [item["text"].strip() for item in details["observations"]]
    if len(set(texts)) != len(texts):
        issue("repeated_observation", "observations", "多个设计维度内容完全相同", "分别描述各维度的可见依据")
    placeholders = {"图中未体现", "无法判断", "不确定", "暂无", "待分析", "配色协调", "构图合理", "设计美观"}
    useful = {item["id"] for item in details["observations"] if item["text"].strip("。 .") not in placeholders and len(item["text"].strip()) >= 10}
    for item in details["inferences"]:
        if not set(item["evidence_ids"]) & useful and item["field"] != "limitations":
            issue("unsupported_inference", item["field"], item["text"], "推断须关联具体可见依据")
    if not set(details["inspiration_card"]["evidence_ids"]) & useful:
        issue("unsupported_method", "inspiration_card", details["inspiration_card"]["method"], "灵感方法须关联具体可见依据")
    brand = result.get("brand", "").strip().casefold()
    if brand and brand not in result.get("ocr_text", "").casefold():
        issue("brand_without_visible_text", "brand", result["brand"], "品牌不能仅凭风格推断；核对可辨认文字")
    factual = re.compile(r"(提升|提高|增长|降低).{0,10}(销量|转化率|点击率|销售额)|(?:销量|转化率|点击率|销售额).{0,10}\d+%|采用.{0,8}(烫金|压纹|覆膜|丝印|环保材料)")
    for field in (*OBSERVATION_FIELDS, *INFERENCE_FIELDS):
        text = result["design"][field]
        if factual.search(text) and not any(word in text for word in ("无法确认", "不能判断", "无法判断", "未提供", "未知", "不代表", "不能证明")):
            issue("unsupported_specific_claim", field, text, "把无证据的效果或生产工艺改为未知")
    for item in details["self_check"]:
        issue("model_reported_issue", item["field"], item["evidence"], item["action"])
    return {"status": "needs_attention" if any(x["severity"] == "block" for x in issues) else "passed", "issues": issues}


def validate_details(raw, result, asset):
    require(isinstance(raw, dict) and set(raw) == DETAIL_FIELDS, "专项分析详情字段无效")
    require(isinstance(raw["profile"], str) and raw["profile"] in PROFILES, "专项分析规则无效")
    _text(raw["route_reason"], 1000)
    observations = _items(raw["observations"], 5)
    require(len(observations) == 5, "专项分析需五项图像观察")
    ids, fields = set(), set()
    for item in observations:
        require(isinstance(item, dict) and set(item) == {"id", "field", "text"}, "图像观察字段无效")
        require(isinstance(item["id"], str) and bool(re.fullmatch(r"o[1-5]", item["id"])) and item["id"] not in ids, "图像观察标识无效")
        require(isinstance(item["field"], str) and item["field"] in OBSERVATION_FIELDS and item["field"] not in fields, "图像观察维度无效")
        _text(item["text"])
        require(item["text"] == result["design"][item["field"]], "观察与旧协议投影不一致")
        ids.add(item["id"]); fields.add(item["field"])
    context = [asset.get("title", "")[:500], asset.get("description", "")[:2000]]
    for item in _items(raw["source_statements"], 8):
        require(isinstance(item, dict) and set(item) == {"text", "source_quote"}, "来源陈述字段无效")
        _text(item["text"]); quote = _text(item["source_quote"], 500)
        require(any(quote in source for source in context), "来源引文不在当前来源资料内")
    inferences = _items(raw["inferences"], 3)
    require(len(inferences) == 3, "专项分析需三项分析推断")
    fields = set()
    for item in inferences:
        require(isinstance(item, dict) and set(item) == {"field", "text", "evidence_ids"}, "分析推断字段无效")
        require(isinstance(item["field"], str) and item["field"] in INFERENCE_FIELDS and item["field"] not in fields, "分析推断维度无效")
        _text(item["text"]); _evidence(item["evidence_ids"], ids)
        require(item["text"] == result["design"][item["field"]], "推断与旧协议投影不一致")
        fields.add(item["field"])
    for item in _items(raw["unknowns"], 10):
        _text(item, 500)
    card = raw["inspiration_card"]
    require(isinstance(card, dict) and set(card) == CARD_FIELDS, "灵感卡字段无效")
    for key in CARD_FIELDS - {"evidence_ids"}:
        _text(card[key])
    _evidence(card["evidence_ids"], ids)
    for key, field in (("method", "reusable_ideas"), ("applicability", "applicability"), ("limitations", "limitations")):
        require(card[key] == result["design"][field], "灵感卡与分析依据不一致")
    for item in _items(raw["self_check"], 8):
        require(isinstance(item, dict) and set(item) == {"field", "evidence", "action"}, "内容自查字段无效")
        for key in item:
            _text(item[key], 500)
    return {**raw, "quality": quality_checks(raw, result)}


def normalize_model_details(raw, result):
    """Expand compact model output into the unchanged stored/UI contract.

    Models never get to overwrite the legacy projection through repeated text.
    Expanded records remain strictly checked at publication time.
    """
    require(isinstance(raw, dict) and set(raw) == DETAIL_FIELDS, "专项分析详情字段无效")
    observations, inferences = [], []
    for item in _items(raw["observations"], 5):
        require(isinstance(item, dict) and set(item) == {"id", "field"}
                and isinstance(item.get("field"), str) and item["field"] in OBSERVATION_FIELDS, "精简图像观察字段无效")
        observations.append({**item, "text": result["design"][item["field"]]})
    for item in _items(raw["inferences"], 3):
        require(isinstance(item, dict) and set(item) == {"field", "evidence_ids"}
                and isinstance(item.get("field"), str) and item["field"] in INFERENCE_FIELDS, "精简分析推断字段无效")
        inferences.append({**item, "text": result["design"][item["field"]]})
    card = raw["inspiration_card"]
    require(isinstance(card, dict) and set(card) == {"task", "evidence_ids"}, "精简灵感卡字段无效")
    return {**raw, "observations": observations, "inferences": inferences,
            "inspiration_card": {**card, "method": result["design"]["reusable_ideas"],
                                 "applicability": result["design"]["applicability"], "limitations": result["design"]["limitations"]}}


def bind_details(raw, result, asset, job, model, usage=None):
    return {**validate_details(normalize_model_details(raw, result), result, asset), **versions(), "job_id": job["id"],
            "source": {"asset_id": asset["id"], "revision": job["revision"], "sha256": asset["sha256"], "input_binding": job["input_binding"]},
            "model_version": model, "usage": usage}


def get_analysis_details(db, job_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_details'").fetchone():
        return None
    row = db.execute("SELECT document FROM analysis_details WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def require_publishable_details(db, job, asset):
    """Caller validates current editorial revision; source revision is the job's.

    Automatic acceptance increments the asset revision, so comparing its revision
    here with the source revision would incorrectly invalidate every accepted item.
    """
    job = dict(job)
    if not details_required(job):
        return None
    details = get_analysis_details(db, job["id"])
    require(isinstance(details, dict), "新版专项分析缺少详情，不能降级自动发布")
    try:
        require(job.get("details_sha256") == details_digest(details), "专项分析详情摘要不一致")
        require(all(details.get(key) == value for key, value in versions().items()), "专项分析规则版本已变化")
        source = details["source"]
        require(details["job_id"] == job["id"] and source["asset_id"] == job["asset_id"] == asset["id"]
                and source["revision"] == job["revision"] and source["sha256"] == asset["sha256"]
                and bool(source["input_binding"]) and source["input_binding"] == job["input_binding"], "专项分析来源绑定失效")
        suggestion = json.loads(job["suggestion"]) if isinstance(job["suggestion"], str) else job["suggestion"]
        require(details["model_version"] == suggestion["model_version"], "专项分析模型绑定失效")
        # Recheck evidence/projection and quality. Source quotes were verified
        # against bounded input before storage; publication proofs seal the digest.
        source_context = {"title": "", "description": ""}
        core = {key: details[key] for key in DETAIL_FIELDS}
        # Avoid treating edited current description as the historical model input.
        core = {**core, "source_statements": []}
        verified = validate_details(core, suggestion, source_context)
        require(details["quality"] == verified["quality"] and verified["quality"]["status"] == "passed", "专项分析内容有疑点，需处理后发布")
    except (KeyError, TypeError, ValueError):
        raise Conflict("专项分析详情无效或已失效，不能自动发布") from None
    return details
