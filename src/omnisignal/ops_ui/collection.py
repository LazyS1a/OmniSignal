"""Allowlisted manual collection page."""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from .presentation import format_timestamp


Load = Callable[[str, dict[str, object] | None], dict[str, object] | None]
Run = Callable[[str], dict[str, object] | None]

STATUS_LABELS = {
    "pending": "排队中",
    "running": "运行中",
    "succeeded": "成功",
    "paused": "已暂停",
    "failed": "失败",
}

READINESS_LABELS = {
    "ready": "可运行",
    "unavailable": "依赖不可用",
    "disabled": "任务已停用",
}


def render_collection(load: Load, *, run: Run | None, save_profile=None) -> None:
    st.title("采集任务")
    st.caption("这里只运行服务端固定白名单任务；页面不能提交脚本、模块名或任意策略路径。")
    if save_profile is not None:
        from .search_profiles import render_profile_editor
        render_profile_editor(load, save_profile)
    tasks_doc = load("/ops/collection-tasks", None)
    jobs_doc = load("/ops/collection-jobs", {"limit": 30})
    schedules_doc = load("/ops/snapshot-schedules", None)
    if tasks_doc is None or jobs_doc is None or schedules_doc is None:
        return

    st.subheader("定时快照")
    if schedules_doc.get("auto_start") is False:
        st.info("全局自动启动已关闭；不会在后台触发采集，也没有注册系统计划任务。")
    schedules = schedules_doc.get("items", [])
    if isinstance(schedules, list) and schedules:
        st.dataframe(
            [
                {
                    "计划": item.get("display_name", "—"),
                    "任务": item.get("task_id", "—"),
                    "状态": "已暂停" if item.get("status") == "paused" else str(item.get("status", "—")),
                    "间隔（分钟）": item.get("interval_minutes", "—"),
                    "下个窗口预览": item.get("next_window_preview", "—"),
                    "会触发": "是" if item.get("eligible_to_trigger") else "否",
                }
                for item in schedules
                if isinstance(item, dict)
            ],
            hide_index=True,
            width="stretch",
        )

    tasks = tasks_doc.get("items", [])
    if not isinstance(tasks, list):
        st.error("采集任务响应格式无效。")
        return
    st.subheader("可运行任务")
    if run is None:
        st.info("当前没有可用的操作身份。请从一键总控台启动，或在左侧输入 operator 令牌。")
    for item in tasks:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id", ""))
        readiness = item.get("readiness") if isinstance(item.get("readiness"), dict) else {}
        dependency_ready = readiness.get("available") is True
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                st.markdown(f"#### {item.get('display_name', task_id)}")
                st.caption(str(item.get("description", "")))
                st.caption(f"来源：{item.get('source_id', '—')} · 任务 ID：{task_id}")
                keyword_set = item.get("keyword_set") if isinstance(item.get("keyword_set"), dict) else {}
                entity_set = item.get("entity_set") if isinstance(item.get("entity_set"), dict) else None
                entity_label = (
                    f"{entity_set.get('id')}@{entity_set.get('version')}" if entity_set else "不适用"
                )
                st.caption(
                    f"关键词集：{keyword_set.get('id', '—')}@{keyword_set.get('version', '—')} · "
                    f"实体集：{entity_label} · 访问层级：{item.get('access_tier', '—')}"
                )
                st.caption(f"观测范围：{item.get('observation_scope', '—')}")
                if item.get("is_example") is True:
                    st.warning("这是示例词配置，结果只用于验证链路，不代表你的产品数据。")
                readiness_status = str(readiness.get("status", "unavailable"))
                readiness_label = READINESS_LABELS.get(readiness_status, "检查失败")
                readiness_message = str(readiness.get("message", "来源状态暂时无法确认。"))
                if dependency_ready:
                    st.success(f"来源状态：{readiness_label} · {readiness_message}")
                else:
                    st.error(f"来源状态：{readiness_label} · {readiness_message}")
                if readiness.get("checked_at"):
                    st.caption(f"状态检查时间：{format_timestamp(readiness['checked_at'])}")
            with right:
                clicked = st.button(
                    "运行一次",
                    key=f"run-collection-{task_id}",
                    type="primary",
                    width="stretch",
                    disabled=run is None or item.get("enabled") is not True or not dependency_ready,
                )
                if clicked and run is not None:
                    result = run(task_id)
                    if result is not None:
                        st.success(f"已进入队列：{str(result.get('job_id', ''))[:8]}")
                        st.rerun()

    st.subheader("最近任务")
    jobs = jobs_doc.get("items", [])
    if not isinstance(jobs, list) or not jobs:
        st.caption("还没有人工触发记录。")
        return
    rows = []
    diagnostic_rows = []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        summary = item.get("result_summary") if isinstance(item.get("result_summary"), dict) else {}
        label = STATUS_LABELS.get(str(item.get("status")), str(item.get("status", "—")))
        if item.get("source_id") == "searxng_results" and item.get("status") == "succeeded":
            label = {"partial": "部分成功", "complete": "完成", "failed": "采集失败"}.get(summary.get("coverage_status"), "完成（覆盖未核验）")
        reason_labels = {"captcha": "验证码限制", "timeout": "上游超时", "rate_limit": "上游限流",
                         "access_denied": "访问受限", "schema_drift": "响应结构变化", "upstream": "上游异常",
                         "dependency_missing": "依赖缺失"}
        for diagnostic in summary.get("diagnostics", []):
            diagnostic_rows.append({"任务": item.get("job_id"), "关键词": diagnostic["query"],
                                    "引擎": diagnostic["engine"], "结果数": diagnostic["record_count"],
                                    "状态": {"complete": "完成", "partial": "部分结果", "empty": "正常返回零结果", "failed": "失败"}[diagnostic["status"]],
                                    "原因": reason_labels.get(diagnostic.get("reason"), "—")})
        rows.append({
            "状态": label,
            "任务": item.get("task_id", "—"),
            "来源": item.get("source_id", "—"),
            "记录数": summary.get("records_seen", "—"),
            "新增": summary.get("inserted", "—"),
            "错误码": item.get("error_code") or "—",
            "创建时间": item.get("created_at") or "—",
            "运行 ID": item.get("run_id") or "—",
        })
    st.dataframe(rows, width="stretch", hide_index=True)
    if diagnostic_rows:
        st.subheader("逐引擎采集诊断")
        st.dataframe(diagnostic_rows, width="stretch", hide_index=True)
    st.caption("完成表示任务执行结束，不保证全部引擎可用。旧任务未保存逐引擎诊断时标记为覆盖未核验。")
