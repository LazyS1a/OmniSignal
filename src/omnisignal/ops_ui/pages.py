"""Bounded Streamlit pages backed exclusively by the operations API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

import streamlit as st

from .client import OpsCsvExport
from .presentation import compact_id, format_ratio, format_timestamp, status_meta


Loader = Callable[[str, Mapping[str, object] | None], dict[str, object] | None]
ControlAction = Callable[..., dict[str, object] | None]
ExportAction = Callable[..., OpsCsvExport | None]


def page_header(title: str, subtitle: str) -> None:
    st.markdown('<div class="os-kicker">OmniSignal Operations</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="os-title">{title}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="os-subtitle">{subtitle}</div>', unsafe_allow_html=True)


def render_overview(load: Loader) -> None:
    page_header("运行总览", "只看系统运行、数据覆盖与质量状态，不评价任何产品。")
    data = load("/ops/summary", {"window_hours": 24})
    if data is None:
        return

    records = _mapping(data.get("records"))
    ingestion = _mapping(data.get("ingestion_runs"))
    coverage = _mapping(data.get("archive_coverage"))
    cols = st.columns(5)
    cols[0].metric("活跃原始记录", _number(records.get("active_ingested")))
    cols[1].metric("标准化版本", _number(records.get("normalized_versions")))
    cols[2].metric("24 小时采集运行", _number(ingestion.get("total")))
    cols[3].metric("停止或隔离", _number(ingestion.get("stopped")))
    cols[4].metric("24 小时插件尝试", _number(data.get("plugin_attempts")))

    st.subheader("原始证据覆盖")
    ratio = coverage.get("ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1:
        st.progress(float(ratio), text=format_ratio(coverage))
    else:
        st.info(format_ratio(coverage))
    st.caption(f"统计范围：{coverage.get('scope', '未返回')}")

    quality = data.get("quality_statuses")
    if isinstance(quality, dict) and quality:
        st.subheader("质量状态分布")
        rows = []
        for name, count in sorted(quality.items()):
            label, _ = status_meta(name)
            rows.append({"状态": label, "记录数": _number(count), "原始状态": str(name)})
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.info("当前没有标准化质量数据。")

    st.markdown(
        '<div class="os-note">这里的“需关注、隔离、重复候选”都是数据工程状态，不是产品好坏、用户情绪或改进建议。</div>',
        unsafe_allow_html=True,
    )


def render_sources(
    load: Loader,
    control: ControlAction | None = None,
    principal: Mapping[str, str] | None = None,
) -> None:
    page_header("数据来源", "核对来源准入、授权期限、速率预算、检查点与最近一次运行。")
    flash = st.session_state.pop("source_control_flash", None)
    if isinstance(flash, str) and flash:
        st.success(flash)
    data = load("/ops/sources", None)
    if data is None:
        return
    items = _items(data)
    if not items:
        st.info("来源登记表目前没有可展示条目。")
        return

    st.caption(
        f"登记版本 {data.get('registry_version', '—')} · 更新时间 {data.get('registry_updated_at', '—')} · 共 {data.get('total', len(items))} 个来源"
    )
    rows: list[dict[str, object]] = []
    for item in items:
        latest = _mapping(item.get("latest_run"))
        registry_label, _ = status_meta(item.get("registry_status"))
        run_label, _ = status_meta(latest.get("status")) if latest else ("从未运行", "neutral")
        effective_enabled = _effective_enabled(item)
        rows.append(
            {
                "来源": item.get("display_name") or item.get("source_id"),
                "来源 ID": item.get("source_id"),
                "准入": registry_label,
                "运行开关": "已启用" if effective_enabled else "已停用",
                "最近运行": run_label,
                "检查点": str(item.get("checkpoint_version")) if item.get("checkpoint_version") is not None else "—",
                "速率/分钟": str(item.get("rate_budget_rpm")) if item.get("rate_budget_rpm") is not None else "—",
                "紧急停用": "已配置" if item.get("kill_switch") is True else "未配置",
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    for item in items:
        source_id = str(item.get("source_id", "unknown"))
        with st.expander(f"{item.get('display_name') or source_id} · {source_id}"):
            latest = _mapping(item.get("latest_run"))
            left, right = st.columns(2)
            left.write(f"连接器类型：{item.get('connector_class') or '—'}")
            left.write(f"授权依据：{item.get('authorization_basis') or '—'}")
            left.write(f"授权到期：{format_timestamp(item.get('authorization_expires_at'))}")
            right.write(f"检查点更新时间：{format_timestamp(item.get('checkpoint_updated_at'))}")
            right.write(f"最近运行：{latest.get('run_id') or '—'}")
            right.write(f"最近完成：{format_timestamp(latest.get('finished_at'))}")
            effective_enabled = _effective_enabled(item)
            control_version = _number(item.get("control_version"))
            st.caption(
                f"运行开关：{'已启用' if effective_enabled else '已停用'} · 控制版本 {control_version}"
            )
            if item.get("control_updated_by"):
                st.caption(
                    f"最近控制人：{item.get('control_updated_by')} · {format_timestamp(item.get('control_updated_at'))}"
                )
            if control is not None and principal is not None and principal.get("role") in {"operator", "admin"}:
                st.divider()
                desired_enabled = not effective_enabled
                action = "ENABLE" if desired_enabled else "DISABLE"
                phrase = f"{action} {source_id}"
                label = "启用后续运行" if desired_enabled else "停用后续运行"
                st.caption("只影响之后启动的新运行；不会强杀已经在途的采集进程。")
                st.code(phrase, language=None)
                with st.form(f"source_control_{source_id}_{control_version}"):
                    reason = st.text_input(
                        "操作原因",
                        max_chars=240,
                        key=f"source_reason_{source_id}_{control_version}",
                    )
                    confirmation = st.text_input(
                        "输入上方确认短语",
                        max_chars=96,
                        key=f"source_confirmation_{source_id}_{control_version}",
                    )
                    submitted = st.form_submit_button(label, type="primary", width="stretch")
                if submitted:
                    result = control(
                        source_id=source_id,
                        enabled=desired_enabled,
                        expected_version=control_version,
                        confirmation=confirmation,
                        reason=reason,
                    )
                    if result is not None:
                        st.session_state["source_control_flash"] = f"{source_id} 已{'启用' if desired_enabled else '停用'}。"
                        st.rerun()


def render_runs(load: Loader) -> None:
    page_header("运行记录", "按采集、标准化和插件三条链路查看可追溯运行历史。")
    kind_labels = {"采集": "ingestion", "标准化": "normalization", "处理插件": "plugin"}
    left, middle, right = st.columns([1, 1, 1])
    selected_kind = left.selectbox("运行类型", list(kind_labels), key="runs_kind")
    source_id = middle.text_input("来源 ID（可选）", max_chars=64, key="runs_source")
    run_status = right.text_input("状态（可选）", max_chars=20, key="runs_status")
    kind = kind_labels[selected_kind]
    data = load(
        "/ops/runs",
        {"kind": kind, "source_id": source_id, "status": run_status, "limit": 100, "offset": 0},
    )
    if data is None:
        return
    items = _items(data)
    st.caption(f"共 {data.get('total', len(items))} 条，当前展示前 {len(items)} 条。")
    if not items:
        st.info("当前筛选条件下没有运行记录。")
        return

    rows = []
    for item in items:
        status_label, _ = status_meta(item.get("status"))
        rows.append(
            {
                "运行 ID": compact_id(item.get("run_id"), 14),
                "类型": selected_kind,
                "来源/插件": item.get("source_id") or item.get("plugin_id") or "—",
                "状态": status_label,
                "输入/看到": str(item.get("input_count", item.get("records_seen", "—"))),
                "输出/写入": str(
                    item.get("normalized_count", item.get("output_count", item.get("records_written", "—")))
                ),
                "开始": format_timestamp(item.get("started_at") or item.get("created_at")),
                "结束": format_timestamp(item.get("finished_at")),
                "错误码": item.get("error_code") or "—",
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    run_ids = [str(item.get("run_id")) for item in items if item.get("run_id")]
    selected_run = st.selectbox(
        "查看运行详情",
        [""] + run_ids,
        format_func=lambda value: "选择一条运行" if not value else compact_id(value, 24),
        key=f"run_detail_{kind}",
    )
    if selected_run:
        detail = load(f"/ops/runs/{kind}/{selected_run}", None)
        if detail is not None:
            st.json(detail, expanded=False)


def render_records(load: Loader, *, prepare_export: ExportAction | None = None) -> None:
    page_header("标准记录", "检索标准化结果并按需展开来源、血缘、质量事件和插件输出。")
    quality_labels = {"全部": "", "可用": "accepted", "需关注": "warning", "已隔离": "quarantined"}
    left, middle, right = st.columns([1, 1, 2])
    source_id = left.text_input("来源 ID（可选）", max_chars=64, key="records_source")
    quality_label = middle.selectbox("质量状态", list(quality_labels), key="records_quality")
    query = right.text_input("标题或正文关键词", max_chars=200, key="records_query")
    data = load(
        "/ops/records",
        {
            "source_id": source_id,
            "quality_status": quality_labels[quality_label],
            "q": query,
            "limit": 100,
            "offset": 0,
        },
    )
    if data is None:
        return
    items = _items(data)
    st.caption(f"共 {data.get('total', len(items))} 条，当前展示前 {len(items)} 条。")
    if prepare_export is not None:
        st.caption("CSV 最多 1000 行；标题最多 500 字、正文最多 4000 字，截断会在文件中标记。")
        if st.button("准备当前筛选 CSV", key="prepare_records_export"):
            export = prepare_export(
                source_id=source_id,
                quality_status=quality_labels[quality_label],
                q=query,
                limit=1000,
            )
            if export is not None:
                st.success(f"已准备 {export.rows} 行标准记录。")
                st.download_button(
                    "下载 CSV",
                    data=export.content,
                    file_name=export.filename,
                    mime="text/csv",
                    key="download_records_export",
                )
    if not items:
        st.info("当前筛选条件下没有标准记录。")
        return

    rows = []
    for item in items:
        status_label, _ = status_meta(item.get("quality_status"))
        rows.append(
            {
                "标题": item.get("title") or "（无标题）",
                "来源": item.get("source_id"),
                "来源记录": compact_id(item.get("source_record_id"), 18),
                "质量": status_label,
                "语言": item.get("language") or "und",
                "正文长度": _number(item.get("text_length")),
                "发布时间": format_timestamp(item.get("published_at")),
                "内容预览": item.get("text_preview") or "—",
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    record_ids = [str(item.get("normalized_id")) for item in items if item.get("normalized_id")]
    selected_record = st.selectbox(
        "查看记录详情",
        [""] + record_ids,
        format_func=lambda value: "选择一条记录" if not value else compact_id(value, 24),
        key="record_detail",
    )
    if selected_record:
        detail = load(f"/ops/records/{selected_record}", None)
        if detail is not None:
            st.subheader(str(detail.get("title") or "无标题记录"))
            st.write(detail.get("text") or "（无正文）")
            with st.expander("血缘与处理详情"):
                st.json(detail, expanded=False)


def render_quality(load: Loader) -> None:
    page_header("数据质量", "展示可用性、隔离和质量事件口径；所有比例都保留分子与分母。")
    data = load("/ops/quality", None)
    if data is None:
        return
    statuses = data.get("quality_statuses")
    if not isinstance(statuses, list) or not statuses:
        st.info("当前没有质量状态数据。")
    else:
        for raw in statuses:
            item = _mapping(raw)
            label, _ = status_meta(item.get("status"))
            with st.container(border=True):
                st.markdown(f"**{label}**")
                ratio = item.get("ratio")
                if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1:
                    st.progress(float(ratio), text=format_ratio(item))
                else:
                    st.caption(format_ratio(item))
                st.caption(f"范围：{item.get('scope', '未返回')}")

    event_codes = data.get("event_codes")
    if isinstance(event_codes, list) and event_codes:
        st.subheader("质量事件")
        rows = []
        for raw in event_codes:
            item = _mapping(raw)
            severity, _ = status_meta(item.get("severity"))
            rows.append(
                {
                    "事件代码": item.get("code"),
                    "级别": severity,
                    "次数": _number(item.get("count")),
                    "范围": item.get("scope"),
                }
            )
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.info("当前没有质量事件。")


def render_audit(load: Loader) -> None:
    page_header("审计记录", "查看系统动作与故障证据；敏感字段和本机路径由服务端统一脱敏。")
    left, right = st.columns(2)
    action = left.text_input("动作（可选）", max_chars=80, key="audit_action")
    target = right.text_input("目标（可选）", max_chars=256, key="audit_target")
    data = load("/ops/audit", {"action": action, "target": target, "limit": 100, "offset": 0})
    if data is None:
        return
    items = _items(data)
    st.caption(f"共 {data.get('total', len(items))} 条，当前展示前 {len(items)} 条。")
    if not items:
        st.info("当前筛选条件下没有审计记录。")
        return

    rows = []
    for item in items:
        rows.append(
            {
                "时间": format_timestamp(item.get("created_at")),
                "动作": item.get("action"),
                "目标": item.get("target"),
                "执行者": item.get("actor"),
                "运行 ID": compact_id(item.get("run_id"), 14),
                "事件 ID": compact_id(item.get("event_id"), 14),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    event_ids = [str(item.get("event_id")) for item in items if item.get("event_id")]
    selected_event = st.selectbox(
        "查看审计详情",
        [""] + event_ids,
        format_func=lambda value: "选择一条审计记录" if not value else compact_id(value, 24),
        key="audit_detail",
    )
    if selected_event:
        selected = next(item for item in items if str(item.get("event_id")) == selected_event)
        st.json(selected, expanded=False)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _items(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = document.get("items")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _effective_enabled(item: Mapping[str, object]) -> bool:
    value = item.get("effective_enabled")
    if isinstance(value, bool):
        return value
    return item.get("registry_status") == "allowed"
