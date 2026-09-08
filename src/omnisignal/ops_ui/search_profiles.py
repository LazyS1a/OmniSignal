"""Tag-based profile editor; no network activity until an explicit save."""
from __future__ import annotations

import streamlit as st
from pydantic import ValidationError

from omnisignal.search_profiles import SearchProfile


def render_profile_editor(load, save) -> None:
    document = load("/ops/search-profiles", None)
    if document is None:
        return
    items = document.get("items", [])
    by_id = {item["task_id"]: item["profile"] for item in items}
    st.subheader("我的检索配置")
    st.caption("输入后按回车添加标签。保存不采集；修改后另存一份，旧配置和历史快照保留。")
    if message := st.session_state.pop("profile_saved_message", None):
        st.success(message)
    selected = st.selectbox("新建 / 从已有配置复制", ["new", *by_id],
                            format_func=lambda key: "新建配置" if key == "new" else f"{by_id[key]['name']} · {key[-6:]}")
    profile = by_id.get(selected, {})
    prefix = f"profile-{selected}"
    name = st.text_input("配置名称（可不填）", value=profile.get("name", ""), max_chars=80,
                         placeholder="例如：耳机竞品观察；不填则用首个关键词", key=f"{prefix}-name")
    queries = st.multiselect("搜索关键词", options=profile.get("queries", []), default=profile.get("queries", []),
                            accept_new_options=True, max_selections=10, key=f"{prefix}-queries",
                            placeholder="输入关键词，按回车添加")
    engines = st.multiselect("搜索来源", options=["duckduckgo", "brave"],
                            default=profile.get("engines", ["duckduckgo", "brave"]),
                            format_func=lambda v: {"duckduckgo": "DuckDuckGo", "brave": "Brave"}[v],
                            key=f"{prefix}-engines")
    existing = profile.get("products", [])
    products = []
    for role, label in (("owned", "自家产品"), ("competitor", "竞品")):
        names = [p["name"] for p in existing if p["role"] == role]
        chosen = st.multiselect(label, names, default=names, accept_new_options=True, max_selections=10,
                                key=f"{prefix}-{role}", placeholder="可不填，仅查看原始结果")
        for name_value in chosen:
            original = next((p for p in existing if p["name"] == name_value and p["role"] == role), {})
            product_key = f"{prefix}-{role}-{name_value}"
            with st.expander(f"{label}：{name_value} · 别名与官网（可选）"):
                aliases = st.multiselect("产品别名", original.get("aliases", []), default=original.get("aliases", []),
                                         accept_new_options=True, max_selections=29, key=f"{product_key}-aliases")
                domains = st.multiselect("官网域名", original.get("domains", []), default=original.get("domains", []),
                                         accept_new_options=True, max_selections=20, key=f"{product_key}-domains",
                                         placeholder="例如 example.com，不填 https:// 或路径")
            products.append({"name": name_value, "role": role, "aliases": aliases, "domains": domains})
    st.caption(f"本次上限：{len(queries) * len(engines) * 10} 条结果；每个词、每个引擎最多 10 条，不代表搜索次数。")
    if st.button("保存配置", key=f"{prefix}-save", type="primary", disabled=save is None):
        try:
            body = SearchProfile(name=name, queries=queries, engines=engines, products=products)
        except ValidationError:
            st.error("请检查：至少一个关键词和来源；词条不能重复或过长，产品总数最多 10 个，别名不能跨产品重复，域名不带协议或路径；配置总大小最多 8 KB。")
            return
        result = save(body.model_dump(mode="json"))
        if result is not None:
            st.session_state["profile_saved_message"] = "配置已保存，未触发采集。请在下方对应任务卡点击“运行一次”。"
            st.rerun()
