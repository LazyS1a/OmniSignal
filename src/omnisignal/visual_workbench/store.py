"""Atomic local storage for visual-project manifests."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile
import threading
from uuid import uuid4

from .contracts import VisualProject, VisualProjectCreate, default_layers


def default_visual_projects_directory(project_root: Path) -> Path:
    explicit = os.getenv("OMNISIGNAL_VISUAL_PROJECTS_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    app_data = os.getenv("OMNISIGNAL_APP_DATA_DIR", "").strip()
    if app_data:
        return (Path(app_data).expanduser() / "visual_projects").resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data) / "OmniSignal" / "visual-projects").resolve()
    return (project_root / "data" / "visual_projects").resolve()


class VisualProjectStore:
    """One API process owns the store; requests never choose filesystem paths."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._lock = threading.RLock()
        self.projects: dict[str, VisualProject] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            if not self.directory.exists():
                return
            for path in sorted(self.directory.glob("visual_*")):
                if not path.is_dir() or path.name in self.projects:
                    continue
                manifest = path / "project.json"
                if not manifest.is_file() or manifest.stat().st_size > 100_000:
                    raise ValueError("visual project manifest is missing or too large")
                project = VisualProject.model_validate_json(manifest.read_bytes())
                if project.project_id != path.name:
                    raise ValueError("visual project directory does not match its manifest")
                self.projects[project.project_id] = project

    def create(self, definition: VisualProjectCreate, *, actor: str) -> VisualProject:
        with self._lock:
            self.reload()
            if len(self.projects) >= 200:
                raise ValueError("visual project limit reached")
            project = VisualProject(
                project_id=f"visual_{uuid4().hex}",
                created_at=datetime.now(timezone.utc),
                created_by=actor,
                definition=definition,
                layers=default_layers(definition.workflow),
            )
            self.directory.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.directory))
            try:
                manifest = staging / "project.json"
                with manifest.open("wb") as handle:
                    handle.write(project.model_dump_json(indent=2).encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                staging.rename(self.directory / project.project_id)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            self.projects[project.project_id] = project
            return project

    def get(self, project_id: str) -> VisualProject | None:
        with self._lock:
            self.reload()
            return self.projects.get(project_id)

    def list(self) -> dict[str, object]:
        with self._lock:
            self.reload()
            items = []
            for project in sorted(self.projects.values(), key=lambda item: item.created_at, reverse=True):
                definition = project.definition
                items.append({
                    "project_id": project.project_id,
                    "name": definition.name,
                    "workflow": definition.workflow.value,
                    "category_keyword": definition.category_keyword,
                    "target_brand": definition.target_brand,
                    "status": project.status,
                    "created_at": project.created_at.isoformat(),
                })
            return {"items": items, "count": len(items)}
