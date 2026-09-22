from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from pydantic import ValidationError
import pytest
import respx
from streamlit.testing.v1 import AppTest

from omnisignal.api import create_app
from omnisignal.api.auth import ControlPrincipal, hash_control_token
from omnisignal.config import Environment, Settings
from omnisignal.ops_ui.client import OpsApiClient
from omnisignal.storage import Base, create_database_engine
from omnisignal.visual_workbench import (
    SourceReference,
    VisualProjectCreate,
    VisualProjectStore,
    default_visual_projects_directory,
)


TOKEN = "visual-project-test-operator-token-000001"


def definition(**changes) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "咖啡视觉实验",
        "workflow": "from_scratch",
        "category_keyword": "咖啡",
        "target_brand": "示例品牌",
        "competitors": ["竞品甲", "竞品乙"],
    }
    value.update(changes)
    return value


def test_visual_contract_builds_distinct_layer_skeletons(tmp_path: Path) -> None:
    store = VisualProjectStore(tmp_path / "scratch")
    scratch = store.create(VisualProjectCreate(**definition()), actor="fixture")
    reverse = VisualProjectStore(tmp_path / "reverse").create(
        VisualProjectCreate(**definition(
            workflow="reverse_rebuild",
            source={
                "usage_basis": "public_reference",
                "platform": "品牌官网",
                "source_url": "https://example.com/post?id=1",
                "note": "仅用于构图分析",
            },
        )),
        actor="fixture",
    )

    assert [layer.layer_id for layer in scratch.layers] == [
        "background", "atmosphere", "product", "decoration", "logo", "headline", "body_text"
    ]
    assert reverse.layers[-2].layer_id == "rebuild_mask"
    assert reverse.layers[-1].layer_id == "evidence"
    assert reverse.layers[-1].editable is False
    assert scratch.analysis_status == scratch.generation_status == "not_requested"
    assert scratch.photoshop_status == "not_configured"


@pytest.mark.parametrize(
    "changes",
    [
        {"workflow": "reverse_rebuild"},
        {"workflow": "reverse_rebuild", "source": {"usage_basis": "public_reference"}},
        {"target_brand": "竞品甲"},
        {"competitors": ["竞品甲", "竞品甲"]},
        {"canvas": {"width": 100, "height": 1350}},
        {"source": {"usage_basis": "owned", "source_url": "file:///D:/private.png"}},
        {"source": {"usage_basis": "owned", "source_url": "https://user:pass@example.com/a"}},
    ],
)
def test_visual_contract_rejects_unsafe_or_ambiguous_scope(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VisualProjectCreate(**definition(**changes))


def test_source_reference_is_metadata_only_and_normalized() -> None:
    source = SourceReference(
        usage_basis="public_reference",
        platform="  品牌   官网 ",
        source_url="https://example.com/reference?item=1",
        note="  只作   分析 ",
    )
    assert source.platform == "品牌 官网"
    assert source.note == "只作 分析"


def test_visual_store_is_atomic_and_reloadable(tmp_path: Path) -> None:
    store = VisualProjectStore(tmp_path / "visual")
    project = store.create(VisualProjectCreate(**definition()), actor="operator-one")
    assert project.project_id.startswith("visual_")
    assert (store.directory / project.project_id / "project.json").is_file()
    assert not list(store.directory.glob(".pending-*"))

    restarted = VisualProjectStore(store.directory)
    loaded = restarted.get(project.project_id)
    assert loaded == project
    assert restarted.list()["count"] == 1
    assert restarted.list()["items"][0]["target_brand"] == "示例品牌"


def test_visual_default_directory_stays_outside_repo_when_app_data_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNISIGNAL_VISUAL_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("OMNISIGNAL_APP_DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    directory = default_visual_projects_directory(tmp_path / "repository")
    assert directory == (tmp_path / "app-data" / "visual_projects").resolve()
    assert tmp_path / "repository" not in directory.parents


def test_visual_api_requires_operator_and_never_executes_adapters(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'visual.db').as_posix()}")
    Base.metadata.create_all(engine)
    app = create_app(
        settings=Settings(environment=Environment.TEST, database_url=str(engine.url)),
        engine=engine,
        search_profiles_path=tmp_path / "profiles",
        visual_projects_path=tmp_path / "visual-projects",
        collection_executor=object(),
        scheduler_runtime_enabled=False,
        control_principals=(
            ControlPrincipal(actor="visual-operator", role="operator", token_sha256=hash_control_token(TOKEN)),
        ),
    )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            capabilities = (await client.get("/ops/visual/capabilities")).json()
            assert capabilities["image_generation"]["automatic_calls"] is False
            assert capabilities["photoshop"]["automatic_writes"] is False
            assert (await client.post("/ops/visual/projects", json=definition())).status_code == 401
            created = await client.post(
                "/ops/visual/projects",
                json=definition(),
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert created.status_code == 200
            project = created.json()
            assert project["created_by"] == "visual-operator"
            assert project["photoshop_status"] == "not_configured"
            listing = (await client.get("/ops/visual/projects")).json()
            assert listing["count"] == 1
            loaded = await client.get(f"/ops/visual/projects/{project['project_id']}")
            assert loaded.json() == project
            assert (await client.get("/ops/visual/projects/visual_bad")).status_code == 404

    asyncio.run(scenario())
    assert len(list((tmp_path / "visual-projects").glob("visual_*"))) == 1
    engine.dispose()


def test_ops_client_posts_visual_project_once_with_ephemeral_token(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post("http://127.0.0.1:8010/ops/visual/projects").mock(
        return_value=httpx.Response(200, json={"project_id": "visual_" + "a" * 32})
    )
    result = OpsApiClient(retries=2).create_visual_project(definition(), bearer_token=TOKEN)
    assert result["project_id"] == "visual_" + "a" * 32
    assert route.call_count == 1
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


def _visual_page_fixture() -> None:
    import streamlit as st
    from omnisignal.ops_ui.visual import render_visual_workbench

    def load(path: str, params=None):
        del params
        if path == "/ops/visual/capabilities":
            return {
                "image_generation": {"status": "not_configured"},
                "photoshop": {"status": "not_configured"},
                "collection": {"status": "not_connected"},
            }
        if path == "/ops/visual/projects":
            return {"items": [], "count": 0}
        raise AssertionError(path)

    def create(body):
        st.session_state["created_visual_body"] = body
        return {"project_id": "visual_" + "b" * 32}

    render_visual_workbench(load, create)


def test_visual_page_creates_metadata_only_skeleton() -> None:
    app = AppTest.from_function(_visual_page_fixture).run(timeout=10)
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["待连接", "待连接", "待连接"]
    app.text_input[0].input("咖啡视觉实验")
    app.text_input[1].input("咖啡")
    app.text_input[2].input("示例品牌")
    app.button[0].click().run(timeout=10)
    assert not app.exception
    body = app.session_state["created_visual_body"]
    assert body["workflow"] == "from_scratch"
    assert body["source"] is None
