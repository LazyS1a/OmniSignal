"""Visual-project skeleton page with explicit no-execution boundaries."""

from __future__ import annotations

import json
from typing import Callable, Mapping

from pydantic import ValidationError
import streamlit as st

from omnisignal.visual_workbench import VisualProjectCreate

from .pages import page_header


Loader = Callable[[str, Mapping[str, object] | None], dict[str, object] | None]
CreateAction = Callable[[dict[str, object]], dict[str, object] | None]
DemoAction = Callable[[], dict[str, object] | None]
UploadAction = Callable[[str, bytes], dict[str, object] | None]
FetchAction = Callable[[str], bytes | None]


def render_visual_workbench(
    load: Loader,
    create_project: CreateAction | None = None,
    create_demo: DemoAction | None = None,
    upload_image: UploadAction | None = None,
    fetch_image: FetchAction | None = None,
    fetch_bundle: FetchAction | None = None,
) -> None:
    page_header("视觉工作台", "创建分层海报示例、上传本地 PNG，并查看每层的制作依据。")
    capabilities = load("/ops/visual/capabilities", None)
    projects = load("/ops/visual/projects", None)
    if capabilities is None or projects is None:
        return

    st.markdown(
        '<div class="os-note">来源地址只保存为引用，不会自动下载；反向流程输出的是重建图层，'
        '不宣称还原原始 PSD。模型与 Photoshop 均需后续显式连接和人工触发。</div>',
        unsafe_allow_html=True,
    )
    image_capability = _mapping(capabilities.get("image_generation"))
    photoshop_capability = _mapping(capabilities.get("photoshop"))
    collection_capability = _mapping(capabilities.get("collection"))
    cols = st.columns(3)
    cols[0].metric("图片模型", _status_label(image_capability.get("status")))
    cols[1].metric("Photoshop", _status_label(photoshop_capability.get("status")))
    cols[2].metric("竞品采集", _status_label(collection_capability.get("status")))

    st.subheader("没有图片？先跑示例")
    st.caption("一键创建虚构咖啡品牌海报：产品、背景、装饰和文字分别保存为 PNG 图层。位置标注来自制作过程。")
    if st.button("创建合成咖啡海报", disabled=create_demo is None):
        result = create_demo() if create_demo else None
        if result is not None:
            st.session_state["visual_created_message"] = "合成示例已创建，可在下方查看预览和下载图层。"
            st.session_state["visual_selected_project"] = result.get("project_id", "")
            st.rerun()

    items = projects.get("items") if isinstance(projects.get("items"), list) else []
    with st.expander("新建图层工程", expanded=not items):
        workflow_label = st.selectbox("制作方式", ["从零按层创作", "参考图反向重建"])
        workflow = "from_scratch" if workflow_label == "从零按层创作" else "reverse_rebuild"
        name = st.text_input("工程名称", max_chars=80, placeholder="例如：咖啡新品视觉实验")
        category = st.text_input("品类关键词", max_chars=80, placeholder="例如：咖啡")
        target_brand = st.text_input("目标品牌", max_chars=80, placeholder="例如：目标咖啡品牌")
        competitors = st.multiselect(
            "参考竞品（可选）", options=[], accept_new_options=True, max_selections=10,
            placeholder="输入品牌名后按回车",
        )
        source_url = ""
        platform = ""
        usage_basis = "public_reference"
        source_note = ""
        if workflow == "reverse_rebuild":
            st.caption("这里只登记参考来源。骨架阶段不下载图片，也不执行自动拆层。")
            source_url = st.text_input("参考图或原帖地址（可留空，但必须填写来源说明）", max_chars=2048)
            platform = st.text_input("来源平台", max_chars=80, placeholder="例如：品牌官网 / 小红书")
            usage_basis = st.selectbox(
                "素材使用依据",
                ["public_reference", "owned", "licensed"],
                format_func=lambda value: {
                    "public_reference": "公开参考，仅作分析",
                    "owned": "自有素材",
                    "licensed": "已获授权素材",
                }[value],
            )
            source_note = st.text_input("来源说明", max_chars=240, placeholder="例如：仅用于构图分析，不进入交付素材")

        if st.button("创建骨架工程", type="primary", disabled=create_project is None):
            source = None
            if workflow == "reverse_rebuild":
                source = {
                    "usage_basis": usage_basis,
                    "platform": platform,
                    "source_url": source_url,
                    "note": source_note,
                }
            try:
                body = VisualProjectCreate(
                    name=name,
                    workflow=workflow,
                    category_keyword=category,
                    target_brand=target_brand,
                    competitors=tuple(competitors),
                    source=source,
                )
            except ValidationError:
                st.error("请填写工程名称、品类和目标品牌；反向重建必须声明素材来源与使用依据。")
            else:
                result = create_project(body.model_dump(mode="json")) if create_project else None
                if result is not None:
                    st.session_state["visual_created_message"] = f"已创建：{result.get('project_id', '')}"
                    st.rerun()

    if message := st.session_state.pop("visual_created_message", None):
        st.success(message)

    st.subheader("视觉工程")
    if not items:
        st.info("还没有视觉工程。创建骨架只写入图层清单，不会产生图片或费用。")
        return
    rows = [
        {
            "工程": item.get("name", ""),
            "流程": "从零创作" if item.get("workflow") == "from_scratch" else "反向重建",
            "品类": item.get("category_keyword", ""),
            "目标品牌": item.get("target_brand", ""),
            "状态": item.get("status", ""),
            "ID": item.get("project_id", ""),
        }
        for item in items if isinstance(item, dict)
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    by_id = {str(item.get("project_id")): item for item in items if isinstance(item, dict)}
    selected = st.selectbox(
        "查看图层清单",
        list(by_id),
        format_func=lambda project_id: f"{by_id[project_id].get('name', project_id)} · {project_id[-6:]}",
        key="visual_selected_project",
    )
    detail = load(f"/ops/visual/projects/{selected}", None)
    if detail is None:
        return
    st.caption(
        f"分析：{detail.get('analysis_status', 'unknown')} · "
        f"生成：{detail.get('generation_status', 'unknown')} · "
        f"Photoshop：{detail.get('photoshop_status', 'unknown')}"
    )
    image_meta = _mapping(detail.get("image"))
    if image_meta:
        st.subheader("图片预览")
        st.caption(
            f"{image_meta.get('width')} × {image_meta.get('height')} px · "
            f"已记录 SHA-256：{str(image_meta.get('original_sha256', ''))[:12]}… · "
            f"{'合成示例' if image_meta.get('kind') == 'demo' else '本地上传'}"
        )
        if fetch_image is not None:
            content = fetch_image(selected)
            if content is not None:
                st.image(content, width=420)
        else:
            st.info("输入操作令牌后可查看图片。")
        regions = detail.get("regions") if isinstance(detail.get("regions"), list) else []
        if regions:
            st.caption("下表是合成时记录的已知位置，不是模型识别结果。")
            st.dataframe([
                {
                    "图层 ID": region.get("layer_id", ""),
                    "位置 X/Y": f"{region.get('x', '')} / {region.get('y', '')}",
                    "宽 × 高": f"{region.get('width', '')} × {region.get('height', '')}",
                }
                for region in regions if isinstance(region, dict)
            ], hide_index=True, width="stretch")
        if image_meta.get("kind") == "demo" and fetch_bundle is not None:
            bundle = fetch_bundle(selected)
            if bundle is not None:
                st.download_button(
                    "下载示例分层 ZIP",
                    data=bundle,
                    file_name=f"{selected}-layers.zip",
                    mime="application/zip",
                )
    elif upload_image is not None:
        uploaded = st.file_uploader("上传本地 PNG（可稍后再做）", type=["png"], key=f"visual_upload_{selected}")
        if uploaded is not None:
            st.caption("上传后保存图片尺寸与 SHA-256；普通上传图暂不自动识别元素。")
            if st.button("保存图片到工程", key=f"visual_save_{selected}"):
                if uploaded.size > 5_000_000:
                    st.error("图片不能超过 5 MB。")
                else:
                    result = upload_image(selected, uploaded.getvalue())
                    if result is not None:
                        st.session_state["visual_created_message"] = "图片已保存到工程。"
                        st.rerun()
    layers = detail.get("layers") if isinstance(detail.get("layers"), list) else []
    st.dataframe(
        [
            {
                "顺序": index + 1,
                "图层": layer.get("name", ""),
                "类型": layer.get("kind", ""),
                "可编辑": "是" if layer.get("editable") else "否",
                "状态": layer.get("status", ""),
                "来源": layer.get("provenance", ""),
                "图层文件": layer.get("asset_file") or "—",
            }
            for index, layer in enumerate(layers) if isinstance(layer, dict)
        ],
        hide_index=True,
        width="stretch",
    )
    st.download_button(
        "下载图层工程 JSON",
        data=json.dumps(detail, ensure_ascii=False, indent=2),
        file_name=f"{selected}.json",
        mime="application/json",
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _status_label(value: object) -> str:
    return {
        "not_configured": "待连接",
        "not_connected": "待连接",
        "ready": "已就绪",
    }.get(str(value), "未核验")
