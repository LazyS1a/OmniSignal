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
