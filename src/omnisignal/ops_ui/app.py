"""Streamlit entry point for the bounded OmniSignal operations console."""

from __future__ import annotations

import os
from typing import Mapping
from uuid import uuid4

import streamlit as st

from omnisignal.ops_ui.client import DEFAULT_API_BASE_URL, OpsApiClient, OpsApiError, OpsCsvExport
from omnisignal.ops_ui.collection import render_collection
from omnisignal.ops_ui.pages import (
    render_audit,
    render_overview,
    render_quality,
    render_records,
    render_runs,
    render_sources,
)
from omnisignal.ops_ui.presentation import safe_api_label
from omnisignal.ops_ui.style import APP_CSS
from omnisignal.ops_ui.visibility import render_visibility
from omnisignal.ops_ui.trends import render_trends


CACHE_TTL_SECONDS = 15


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_ops_data(
    api_base_url: str,
    path: str,
    params: tuple[tuple[str, object], ...],
) -> dict[str, object]:
    return OpsApiClient(base_url=api_base_url).get_json(path, params=dict(params))


def main() -> None:
    st.set_page_config(
        page_title="OmniSignal 总控台",
        page_icon="🔭",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(APP_CSS, unsafe_allow_html=True)

    raw_base_url = os.getenv("OMNISIGNAL_API_BASE_URL", DEFAULT_API_BASE_URL)
    try:
        api_base_url = OpsApiClient(base_url=raw_base_url).base_url
    except ValueError:
        st.error("OMNISIGNAL_API_BASE_URL 配置无效：只允许不带账号、路径和参数的 HTTP(S) 地址。")
        return
    local_console_token = os.getenv("OMNISIGNAL_LOCAL_CONSOLE_TOKEN", "")

    def load(path: str, params: Mapping[str, object] | None = None) -> dict[str, object] | None:
        normalized_params = tuple(sorted((params or {}).items()))
        try:
            return load_ops_data(api_base_url, path, normalized_params)
        except (OpsApiError, ValueError) as exc:
            st.error(str(exc))
            st.caption("页面没有使用演示数字填充。请检查 API 是否启动，修复后点击左侧“刷新数据”。")
            return None

    st.sidebar.markdown("## OmniSignal")
    st.sidebar.caption("本地数据与运行总控台")
    st.sidebar.caption(f"API：{safe_api_label(api_base_url)}")
    st.sidebar.caption(f"缓存：{CACHE_TTL_SECONDS} 秒")
    if st.sidebar.button("刷新数据", type="primary", width="stretch"):
        load_ops_data.clear()
        st.rerun()
    st.sidebar.divider()
    st.sidebar.markdown("### 控制权限")

    def clear_control_token() -> None:
        st.session_state["ops_control_token"] = ""

    control_token = st.sidebar.text_input(
        "操作令牌（可选）",
        type="password",
        key="ops_control_token",
        help="只保留在当前界面会话，不进入缓存、URL 或项目文件。",
    )
    principal: dict[str, str] | None = None
    if control_token:
        try:
            identity = OpsApiClient(base_url=api_base_url, retries=0).whoami(control_token)
        except (OpsApiError, ValueError) as exc:
            st.sidebar.error(str(exc))
        else:
            actor = identity.get("actor")
            role = identity.get("role")
            if isinstance(actor, str) and isinstance(role, str):
                principal = {"actor": actor, "role": role}
                st.sidebar.success(f"{actor} · {role}")
        st.sidebar.button(
            "清除操作令牌",
            width="stretch",
            on_click=clear_control_token,
        )
    else:
        if local_console_token:
            st.sidebar.caption("本地采集执行身份已就绪；来源启停仍需手动 operator 令牌。")
        else:
            st.sidebar.caption("未登录控制面；当前只能查看。")
    st.sidebar.divider()
    st.sidebar.caption("支持保存检索配置与有界 CSV 导出；不提供强杀运行、自动重试或删除。")

    def control_source(
        *,
        source_id: str,
        enabled: bool,
        expected_version: int,
        confirmation: str,
        reason: str,
    ) -> dict[str, object] | None:
        if principal is None or not control_token:
            st.error("控制身份不可用，请重新输入操作令牌。")
            return None
        try:
            result = OpsApiClient(base_url=api_base_url, retries=0).set_source_control(
                source_id=source_id,
                enabled=enabled,
                expected_version=expected_version,
                confirmation=confirmation,
                reason=reason,
                idempotency_key=f"ui-{uuid4()}",
                bearer_token=control_token,
            )
        except (OpsApiError, ValueError) as exc:
            st.error(str(exc))
            st.caption("操作没有自动重试。请先刷新状态，再判断是否需要重新提交。")
            return None
        load_ops_data.clear()
        return result

    def prepare_records_export(
        *,
        source_id: str,
        quality_status: str,
        q: str,
        limit: int,
    ) -> OpsCsvExport | None:
        try:
            return OpsApiClient(
                base_url=api_base_url,
                max_response_bytes=10 * 1024 * 1024,
            ).download_records_csv(
                source_id=source_id,
                quality_status=quality_status,
                q=q,
                limit=limit,
            )
        except (OpsApiError, ValueError) as exc:
            st.error(str(exc))
            st.caption("导出文件没有在服务器落盘，也没有用演示内容替代。")
            return None

    def run_collection(task_id: str) -> dict[str, object] | None:
        bearer = control_token or local_console_token
        if not bearer:
            st.error("没有可用的 operator 身份。")
            return None
        try:
            result = OpsApiClient(base_url=api_base_url, retries=0).start_collection_job(
                task_id=task_id,
                idempotency_key=f"ui-collection-{uuid4()}",
                bearer_token=bearer,
            )
        except (OpsApiError, ValueError) as exc:
            st.error(str(exc))
            st.caption("任务提交不会自动重试。请刷新最近任务后再判断。")
            return None
        load_ops_data.clear()
        return result

    def save_profile(profile: dict[str, object]) -> dict[str, object] | None:
        try:
            result = OpsApiClient(base_url=api_base_url, retries=0).save_search_profile(
                profile, bearer_token=control_token or local_console_token)
        except (OpsApiError, ValueError) as exc:
            st.error(str(exc))
            st.caption("保存不会触发采集；结果不确定时可重新保存同一内容，不会创建重复配置。")
            return None
        load_ops_data.clear()
        return result

    def overview_page() -> None:
        render_overview(load)

    def sources_page() -> None:
        can_control = principal is not None and principal.get("role") in {"operator", "admin"}
        render_sources(
            load,
            control=control_source if can_control else None,
            principal=principal,
        )

    def runs_page() -> None:
        render_runs(load)

    def collection_page() -> None:
        render_collection(load, run=run_collection if (control_token or local_console_token) else None,
                          save_profile=save_profile if (control_token or local_console_token) else None)

    def records_page() -> None:
        render_records(load, prepare_export=prepare_records_export)

    def quality_page() -> None:
        render_quality(load)

    def visibility_page() -> None:
        render_visibility(load)

    def trends_page() -> None:
        render_trends(load)

    def audit_page() -> None:
        render_audit(load)

    pages = {
        "运行与来源": [
            st.Page(overview_page, title="运行总览", icon="📊", default=True),
            st.Page(sources_page, title="数据来源", icon="🔌"),
            st.Page(collection_page, title="采集任务", icon="▶️"),
            st.Page(runs_page, title="运行记录", icon="🧭"),
        ],
        "数据与证据": [
            st.Page(trends_page, title="多日趋势", icon="📈"),
            st.Page(visibility_page, title="检索采样", icon="🔎"),
            st.Page(records_page, title="标准记录", icon="🗂️"),
            st.Page(quality_page, title="数据质量", icon="🧪"),
            st.Page(audit_page, title="审计记录", icon="🛡️"),
        ],
    }
    navigation = st.navigation(pages, position="sidebar", expanded=True)
    navigation.run()


if __name__ == "__main__":
    main()
