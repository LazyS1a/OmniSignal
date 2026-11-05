"""Read-only source operations projection. Parser support is not collection evidence."""
from datetime import datetime, timezone
import json
import os

from .acquisition import progress, source_budget
from .store import sources

SESSION_PREFIX = "source.session.v1:"
SESSION_FRESH_SECONDS = 15 * 60
SESSION_REASONS = {
    "session_missing": "未配置采集会话", "session_expired": "会话已过期，请正常登录后检查",
    "session_stale": "会话检查已过期，当前可用性未知", "access_barrier": "来源要求人工验证，自动采集已停止",
    "login_required": "需要正常登录来源网站", "browser_unavailable": "浏览器执行器暂不可用",
    "session_invalid": "会话状态记录无效，等待重新检查", "not_checked": "会话尚未检查",
    "challenge": "来源要求人工验证，自动采集已停止", "robots_denied": "来源不允许自动访问，采集已停止",
    "identity_conflict": "浏览器会话身份与配置不一致，采集已停止", "request_limit": "本次浏览器请求额度已用完",
}


def _instant(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _session(source, raw, now):
    source_id = source["id"]
    public = set(os.getenv("OMNISIGNAL_WORKBENCH_PUBLIC_HTML_SOURCE_IDS", "").split(","))
    if raw is None:
        readiness = source.get("readiness") if isinstance(source.get("readiness"), dict) else {}
        if readiness.get("transport") == "browser":
            return {"kind": "browser", "state": "unknown", "checked_at": None, "expires_at": None, "reason_code": "not_checked", "label": "浏览器已配置，会话尚未检查"}
        if source_id == "pexels":
            configured = bool(os.getenv("OMNISIGNAL_PEXELS_API_KEY_FILE", "").strip() or os.getenv("OMNISIGNAL_PEXELS_API_KEY", "").strip())
            return {"kind": "api", "state": "unknown" if configured else "not_configured", "checked_at": None, "expires_at": None,
                    "reason_code": "not_checked" if configured else "session_missing", "label": "API 凭据已配置，连通性未知" if configured else "API 凭据未配置"}
        if source_id in {"pinterest", "huaban", "zcool", "behance"} and source_id not in public:
            return {"kind": "browser", "state": "not_configured", "checked_at": None, "expires_at": None, "reason_code": "session_missing", "label": "浏览器会话未配置"}
        return {"kind": "manual" if source_id == "manual" else "public_http", "state": "not_required", "checked_at": None, "expires_at": None,
                "reason_code": None, "label": "不使用登录会话；连通性以运行记录为准"}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or len(raw) > 8000:
            raise ValueError()
        state = value.get("state")
        if state not in {"ready", "unknown", "not_configured", "expired", "blocked", "configured"}:
            raise ValueError()
        checked, expires = _instant(value.get("checked_at")), _instant(value.get("expires_at"))
        reason = value.get("reason_code") if value.get("reason_code") in SESSION_REASONS else "not_checked"
        if state == "configured":
            state = "unknown"
        if state == "ready" and (checked is None or checked > now + 60 or now - checked > SESSION_FRESH_SECONDS):
            state, reason = "unknown", "session_stale"
        if state == "ready" and value.get("expires_at") is not None and expires is None:
            state, reason = "unknown", "session_invalid"
        if state == "ready" and expires is not None and expires <= now:
            state, reason = "expired", "session_expired"
        labels = {"ready": "最近会话检查通过", "unknown": "会话状态未知", "not_configured": "浏览器会话未配置", "expired": "会话已过期", "blocked": "会话访问受阻"}
        return {"kind": "browser", "state": state, "checked_at": value.get("checked_at") if checked is not None else None,
                "expires_at": value.get("expires_at") if expires is not None else None, "reason_code": reason, "label": labels[state]}
    except (ValueError, TypeError, OverflowError):
        return {"kind": "browser", "state": "unknown", "checked_at": None, "expires_at": None, "reason_code": "session_invalid", "label": "会话状态未知"}


def _failure(run):
    # Only project-owned fixed messages may cross the API; a raw provider reason may contain private data.
    from .collection import ERRORS
    reason = run.get("reason") or ""
    code = next((key for key, message in ERRORS.items() if reason == message or reason == key), None)
    return {"at": run.get("finished_at") or run.get("started_at"), "code": code or "collection_not_completed",
            "label": ERRORS[code] if code else "本次采集未完整完成；原始诊断未公开，请管理员查看运行日志。"}


def operational_sources(store, *, catalog=None, worker_enabled=None):
    """Return only source-scoped counters and safe session metadata; never creates plans or jobs."""
    catalog = sources() if catalog is None else catalog
    now = store.clock()
    try:
        totals = progress(store)
    except ValueError:
        totals = None
    with store.db() as db:
        # A read transaction gives every source the same plan/run/session snapshot.
        db.execute("BEGIN")
        plans = []
        for row in db.execute("SELECT id,document,enabled FROM plans"):
            value = json.loads(row["document"])
            plans.append({"id": row["id"], "source_id": value.get("source_id"), "enabled": bool(row["enabled"])})
        runs = [dict(row) for row in db.execute("SELECT plan_id,status,started_at,finished_at,reason,imported FROM runs ORDER BY started_at DESC,rowid DESC")]
        sessions = {row["key"][len(SESSION_PREFIX):]: row["value"] for row in db.execute("SELECT key,value FROM meta WHERE key LIKE ?", (SESSION_PREFIX + "%",))}
        heartbeat = db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
    try:
        healthy = bool(heartbeat and 0 <= now - float(heartbeat[0]) < 180 and worker_enabled is not False)
    except (ValueError, OverflowError):
        healthy = False
    result = []
    for source in catalog:
        source_id = source["id"]
        own = [plan for plan in plans if plan["source_id"] == source_id]
        plan_ids = {plan["id"] for plan in own}
        own_runs = [run for run in runs if run["plan_id"] in plan_ids]
        successful = [run for run in own_runs if run["status"] in {"ok", "completed", "success", "partial"} and run["imported"] > 0]
        success_days = set()
        for run in successful:
            instant = _instant(run.get("finished_at") or run.get("started_at"))
            if instant is not None:
                success_days.add(datetime.fromtimestamp(instant + 8 * 3600, timezone.utc).date().isoformat())
        failure = next((run for run in own_runs if run["status"] in {"failed", "partial", "interrupted", "stopped"}), None)
        session = _session(source, sessions.get(source_id), now)
        quota_error = None
        try:
            if totals is None:
                raise ValueError()
            quota = source_budget(store, source_id, totals=totals)
        except ValueError:
            quota, quota_error = None, "来源配额配置无效，当前剩余额度未知。"
        enabled = sum(plan["enabled"] for plan in own)
        latest = own_runs[0] if own_runs else None
        state, label = "waiting", "等待首次计划执行"
        if source_id == "manual":
            state, label = "manual", "手工导入入口"
        elif not own:
            state, label = "no_plans", "尚未创建正式采集计划"
        elif not enabled:
            state, label = "paused", "所有计划已暂停"
        elif session["state"] in {"not_configured", "expired", "blocked", "unknown"}:
            state, label = "session_unavailable", session["label"]
        elif not healthy:
            state, label = "worker_unavailable", "采集执行器尚未在线"
        elif latest and latest["status"] == "running":
            state, label = "running", "计划正在采集"
        elif latest and latest["status"] in {"failed", "partial", "interrupted", "stopped"}:
            state, label = "last_run_issue", "最近运行未完整完成"
        elif successful:
            state, label = "has_collection_history", "已有计划采集入池记录"
        result.append({**source, "operations": {
            "plan_count": len(own), "enabled_plan_count": enabled,
            "last_collection_at": (latest.get("finished_at") or latest.get("started_at")) if latest else None,
            "last_success_at": (successful[0].get("finished_at") or successful[0].get("started_at")) if successful else None,
            "today": {"day": totals["date"] if totals else None, "timezone": "Asia/Shanghai", "imported": totals["by_source"].get(source_id, 0) if totals else None,
                      "scope": "collector_distinct_image_content"},
            "last_failure": _failure(failure) if failure else None, "session": session,
            "state": state, "label": label, "continuous_collection_verified": None,
            "collection_evidence": {"successful_runs": len(successful), "successful_days": len(success_days),
                                    "level": "multiple_days" if len(success_days) > 1 else "limited_runs" if successful else "none"},
            "quota": quota, "quota_error": quota_error,
        }})
    return result
