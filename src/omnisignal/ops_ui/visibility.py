"""Read-only snapshot browser using the shared API loader."""
import streamlit as st
from .pages import page_header
from .presentation import format_timestamp


def render_visibility(load):
    page_header("检索采样", "查看品牌出现次数、有效分母和逐条证据。")
    source = st.radio("样本来源", ["YouTube", "Web / SearXNG"], horizontal=True)
    if source == "Web / SearXNG":
        _render_web_visibility(load)
        return
    _render_youtube_visibility(load)


def _render_youtube_visibility(load):
    st.caption("YouTube 视频提取顺序 · 地区未固定 · 不包含完整页面广告曝光 · 比例仅代表该次样本")
    with st.expander("产品配置入口（可稍后填写）"):
        st.write("自家产品、竞品、别名与官方频道 ID 保存在策略 YAML；通过 --policy 指定文件。当前页面只读。")
        st.code(".\\scripts\\run-youtube-visibility.ps1 -Policy 'examples\\policies\\youtube_visibility.yaml'", language="powershell")
    query = st.text_input("按搜索词筛选", max_chars=120)
    page = int(st.number_input("页码", min_value=1, max_value=5001, value=1, step=1))
    data = load("/ops/visibility", {"q": query, "limit": 20, "offset": (page - 1) * 20})
    if data is None:
        return
    latest = data.get("latest_source_run") or {}
    if latest.get("status") in {"failed", "paused", "quarantined"}:
        st.warning("该来源最近一次运行未成功。下方保留历史快照，请核对采集时间。")
    items = data.get("items", [])
    if not items:
        st.info("当前数据库或筛选范围没有检索快照。试验库的数据不会自动进入主库。")
        return
    st.caption(f"共 {data.get('total', 0)} 个快照，本页 {len(items)} 个；按入库时间倒序。")
    valid = [item for item in items if item.get("validation_status") == "valid"]
    if len(valid) != len(items):
        st.warning("本页存在字段或统计异常的快照，已隔离展示；不会用异常数据计算百分比。")
    if not valid:
        return
    options = {item["snapshot_id"]: item for item in valid}
    selected = st.selectbox("选择快照", list(options), format_func=lambda key:
        f"{'[示例] ' if options[key]['is_example'] else ''}{options[key]['query']} · {format_timestamp(options[key]['fetched_at'])} · {key[:8]}")
    detail = load(f"/ops/visibility/{selected}", None)
    if detail is None:
        return
    if detail["is_example"]:
        st.warning("示例品牌配置：这些角色不代表你的自家产品或真实竞品。")
    if detail["older_than_24h"]:
        st.warning("该快照采集已超过 24 小时，仅供历史查看。")
    st.caption(f"采集时间：{format_timestamp(detail['fetched_at'])} · 请求语言：{detail['requested_language']}")
    complete = detail["quality_status"] == "complete"
    if not complete:
        st.warning("采样不完整：保留命中次数，暂不展示百分比。")
    st.metric("有效视频 / 请求条数", f"{detail['valid_result_count']} / {detail['requested_top_k']}")
    rows = []
    for brand in detail["brands"]:
        value = brand["sample_share_percent"] if complete else None
        rows.append({"品牌": brand["name"], "角色": "自家" if brand["role"] == "owned" else "竞品",
                     "命中条数": brand["count"], "有效分母": brand["denominator"],
                     "采样占比": f"{value:g}%" if value is not None else "不可计算",
                     "首次位置": str(brand["first_position"]) if brand["first_position"] is not None else "未命中",
                     "官方频道结果": brand["official_count"], "第三方标题提及": brand["third_party_title_count"],
                     "来源身份未核实": brand["unverified_origin_count"]})
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("同条结果可以匹配多个品牌；各品牌百分比之和可能超过 100%。内容匹配仅检查标题，另按已配置的官方频道 ID 识别品牌结果。")
    st.subheader("逐条证据")
    names = {brand["brand_id"]: brand["name"] for brand in detail["brands"]}
    evidence = [{"位置": e["position"], "标题": e["title"], "命中品牌": "、".join(names[m["brand_id"]] for m in e["matches"]) or "未命中",
                 "匹配别名": "、".join(a for m in e["matches"] for a in m["aliases"]), "原始链接": e["url"]} for e in detail["evidence"]]
    if evidence:
        st.dataframe(evidence, hide_index=True, width="stretch", column_config={"原始链接": st.column_config.LinkColumn("原始链接")})
    else:
        st.info("没有可展示的视频证据。")
    with st.expander("版本与归档追溯"):
        st.text(f"采集器：{detail['collector_version']}\n规则哈希：{detail['rule_hash']}\n归档 SHA：{detail.get('raw_archive_sha256') or '未归档'}")


def _render_web_visibility(load):
    st.caption("SearXNG 按搜索引擎分别采样 · 每个关键词 × 引擎独立计算 · 不代表真实搜索次数或全网排名")
    query = st.text_input("按 Web 搜索词筛选", max_chars=120)
    page = int(st.number_input("Web 页码", min_value=1, max_value=5001, value=1, step=1))
    data = load("/ops/web-visibility", {"q": query, "limit": 20, "offset": (page - 1) * 20})
    if data is None:
        return
    latest = data.get("latest_source_run") or {}
    if latest.get("status") in {"failed", "paused", "quarantined"}:
        st.warning("SearXNG 最近一次运行未成功。下方保留历史快照，请核对采集时间。")
    items = data.get("items", [])
    if not items:
        st.info("当前数据库或筛选范围没有 SearXNG 检索快照。")
        return
    st.caption(f"共 {data.get('total', 0)} 个快照，本页 {len(items)} 个；按入库时间倒序。")
    valid = [item for item in items if item.get("validation_status") == "valid"]
    if len(valid) != len(items):
        st.warning("本页存在结构或观测上下文异常的快照，已阻止展示其统计。")
    if not valid:
        return
    options = {item["snapshot_id"]: item for item in valid}
    selected = st.selectbox(
        "选择 Web 快照",
        list(options),
        format_func=lambda key: (
            f"{'[示例] ' if options[key]['is_example'] else ''}"
            f"{options[key]['result_count']} 条 · {format_timestamp(options[key]['observed_at'])} · {key[:8]}"
        ),
    )
    detail = load(f"/ops/web-visibility/{selected}", None)
    if detail is None:
        return
    if detail["is_example"]:
        st.warning("这是示例关键词快照，不代表你的产品或真实竞品数据。")
    if detail["older_than_24h"]:
        st.warning("该快照采集已超过 24 小时，仅供历史查看。")
    st.caption(
        f"采集时间：{format_timestamp(detail['observed_at'])} · "
        f"关键词集：{detail['keyword_set']['id']}@{detail['keyword_set']['version']} · "
        f"访问层级：{detail['access_tier']}"
    )
    slices = detail.get("slices", [])
    if detail.get("missing_slices"):
        missing = "；".join(f"{item['query']} · {item['engine']}" for item in detail["missing_slices"])
        st.warning(f"本次采样覆盖不完整，以下组合没有返回记录：{missing}。缺失不代表品牌出现次数为零；下方占比仅适用于有结果的独立切片。")
    if not slices:
        st.info("该快照没有可展示的关键词与引擎切片。")
        return
    slice_options = {f"{item['query']}\0{item['engine']}": item for item in slices}
    selected_slice = st.selectbox(
        "选择关键词与搜索引擎",
        list(slice_options),
        format_func=lambda key: f"{slice_options[key]['query']} · {slice_options[key]['engine']}",
    )
    current = slice_options[selected_slice]
    st.metric("有效结果数", current["denominator"])
    if current["quality_status"] != "complete":
        st.warning("该切片采样不完整：保留结果，但不发布样本占比。")
    entities = current.get("entities", [])
    if entities:
        st.dataframe(
            [
                {
                    "实体": item["name"],
                    "角色": {"owned": "自家", "competitor": "竞品", "reference": "参考"}.get(
                        item["role"], item["role"]
                    ),
                    "命中条数": item["count"],
                    "有效分母": item["denominator"],
                    "采样占比": (
                        f"{item['sample_share_percent']:g}%"
                        if item["sample_share_percent"] is not None else "不可计算"
                    ),
                    "首次位置": item["first_position"] if item["first_position"] is not None else "未命中",
                }
                for item in entities
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("当前快照未绑定实体集，先展示原始检索证据；配置自家与竞品后才会计算样本占比。")
    st.subheader("逐条 Web 证据")
    evidence = [
        {
            "位置": item["position"],
            "标题": item["title"],
            "命中实体": "、".join(match["entity_id"] for match in item["matches"]) or "未配置/未命中",
            "摘要": item["text_preview"],
            "原始链接": item["url"],
        }
        for item in current["results"]
    ]
    st.dataframe(
        evidence,
        hide_index=True,
        width="stretch",
        column_config={"原始链接": st.column_config.LinkColumn("原始链接")},
    )
    with st.expander("范围与归档追溯"):
        st.text(
            f"范围：{detail['scope']}\n"
            f"引擎：{', '.join(detail['engines'])}\n"
            f"归档 SHA：{detail['raw_archive_sha256']}"
        )
