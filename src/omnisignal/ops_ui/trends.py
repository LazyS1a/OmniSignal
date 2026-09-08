"""Read-only trend page preserving metric units, scope and coverage."""

from __future__ import annotations

import streamlit as st

from .pages import page_header
from .presentation import format_timestamp


METRIC_LABELS = {
    "全部": "all",
    "相对兴趣 0-100": "relative_interest",
    "联想词排名": "suggestion_rank",
    "视频样本命中数": "video_result_count",
    "视频样本占比": "video_sample_share",
}


def render_trends(load) -> None:
    page_header("多日趋势", "比较已经落库的观测序列；不同单位和范围不会被强行相加。")
    st.caption("相对兴趣、联想排名和视频样本占比是三种不同口径；这里没有全网真实搜索次数。")
    left, middle, right = st.columns([1, 1.3, 2])
    days = left.selectbox("时间范围", [30, 90, 180, 365], index=1, format_func=lambda value: f"最近 {value} 天")
    metric_label = middle.selectbox("指标", list(METRIC_LABELS))
    query = right.text_input("关键词或实体（可选）", max_chars=120)
    data = load("/ops/trends", {"days": days, "metric_type": METRIC_LABELS[metric_label], "q": query})
    if data is None:
        return
    if data.get("row_scan_truncated") is True:
        st.warning("记录扫描达到服务端上限；当前结果不是完整窗口。")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        st.info("当前时间范围没有可展示的趋势数据。暂停计划不会自动产生新快照。")
        return

    chart_rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = f"{item.get('query')} · {item.get('subject') or item.get('platform')}"
        for point in item.get("points", []):
            if isinstance(point, dict) and isinstance(point.get("value"), (int, float)):
                chart_rows.append({"时间": point.get("observed_at"), "值": point.get("value"), "序列": label})
    units = {item.get("unit") for item in items if isinstance(item, dict)}
    if chart_rows and len(units) == 1:
        st.line_chart(chart_rows, x="时间", y="值", color="序列")
    elif len(units) > 1:
        st.info("当前筛选包含多个单位，为避免误导不叠加画图；请按指标筛选后查看折线。")

    st.caption(f"窗口：{format_timestamp(data.get('window', {}).get('from'))} 至 {format_timestamp(data.get('window', {}).get('to'))} · {len(items)} 条序列")
    for item in items:
        if not isinstance(item, dict):
            continue
        title = f"{item.get('query')} · {item.get('subject') or item.get('platform')}"
        with st.expander(title):
            cols = st.columns(4)
            cols[0].metric("观测点", item.get("sample_count", 0))
            cols[1].metric("覆盖日期", item.get("distinct_days", 0))
            cols[2].metric("单位", item.get("unit", "—"))
            cols[3].metric("可画趋势", "是" if item.get("trend_ready") else "样本不足")
            st.caption(f"范围：{item.get('scope', '—')} · 来源：{item.get('source_id', '—')} · 平台：{item.get('platform', '—')}")
            context = item.get("observation_context") if isinstance(item.get("observation_context"), dict) else {}
            provenance = context.get("provenance_status")
            if provenance != "versioned":
                st.warning("这是版本化观测配置上线前的历史记录；口径可读，但没有关键词集/实体集版本追溯。")
            coverage = item.get("coverage") if isinstance(item.get("coverage"), dict) else {}
            st.json({"观测配置": context, "覆盖说明": coverage}, expanded=False)
            rows = []
            for point in item.get("points", []):
                if not isinstance(point, dict):
                    continue
                rows.append({
                    "时间": point.get("observed_at"),
                    "值": point.get("value"),
                    "分子": point.get("numerator"),
                    "分母": point.get("denominator"),
                    "质量": point.get("quality_status"),
                    "未完成周期": point.get("is_partial"),
                })
            st.dataframe(rows, hide_index=True, width="stretch")
