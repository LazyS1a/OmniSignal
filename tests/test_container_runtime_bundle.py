from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_dockerfile_bundles_every_fixed_task_dependency() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    tasks = yaml.safe_load((ROOT / "config" / "collection_tasks.yaml").read_text(encoding="utf-8"))["tasks"]

    required = {
        "config/collection_tasks.yaml",
        "config/keyword_sets.yaml",
        "config/entity_sets.yaml",
        "config/snapshot_schedules.yaml",
        *(task["policy"] for task in tasks),
    }
    missing = sorted(path for path in required if path not in dockerfile)
    assert missing == []


def test_read_only_api_has_a_separate_writable_application_data_mount() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]

    assert api["read_only"] is True
    mounts = {entry["target"]: entry for entry in api["volumes"]}
    assert mounts["/app/data"]["type"] == "bind"
    assert "OMNISIGNAL_APP_DATA_DIR" in mounts["/app/data"]["source"]


def test_compose_bundles_local_only_streamlit_console() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]
    ui = compose["services"]["ui"]

    assert api["image"] == ui["image"]
    assert ui["depends_on"]["api"]["condition"] == "service_healthy"
    assert ui["ports"] == ["127.0.0.1:${OMNISIGNAL_UI_PORT:-8501}:8501"]
    assert ui["environment"]["OMNISIGNAL_API_BASE_URL"] == "http://api:8000"
    assert ui["environment"]["OMNISIGNAL_LOCAL_CONSOLE_TOKEN"] == "${OMNISIGNAL_LOCAL_CONSOLE_TOKEN:-}"
    assert api["environment"]["OMNISIGNAL_CONTROL_PRINCIPALS_JSON"] == "${OMNISIGNAL_CONTROL_PRINCIPALS_JSON:-}"
    assert ui["read_only"] is True
    assert ui["cap_drop"] == ["ALL"]


def test_application_image_contains_streamlit_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--extra signals --extra ui" in dockerfile
    assert "EXPOSE 8000 8501" in dockerfile


def test_ci_scans_the_image_built_by_compose() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    image = compose["services"]["api"]["image"]
    assert image == "omnisignal-app:${OMNISIGNAL_VERSION:-0.2.0}"
    assert "image-ref: omnisignal-app:0.2.0" in workflow
