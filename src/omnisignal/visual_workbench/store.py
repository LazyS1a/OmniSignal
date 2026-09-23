"""Atomic local storage for visual-project manifests."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import shutil
import tempfile
import threading
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from .contracts import VisualProject, VisualProjectCreate, default_layers
from .demo import make_demo_poster
from .image import MAX_STORED_BYTES, normalize_png


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
            self._persist_new(project, {})
            return project

    def create_demo(self, *, actor: str) -> VisualProject:
        poster = make_demo_poster()
        preview, image = normalize_png(poster.preview, kind="demo")
        definition = VisualProjectCreate(
            name="NOVA BREW · 合成咖啡海报",
            workflow="from_scratch",
            category_keyword="咖啡",
            target_brand="NOVA BREW（虚构）",
            canvas={"width": 1080, "height": 1350},
        )
        with self._lock:
            self.reload()
            if len(self.projects) >= 200:
                raise ValueError("visual project limit reached")
            layers = tuple(layer.model_copy(update={
                "status": "ready",
                "provenance": "synthetic_composition",
                "asset_file": f"layers/{layer.layer_id}.png",
                "asset_sha256": sha256(poster.layers[layer.layer_id]).hexdigest(),
            }) for layer in default_layers(definition.workflow))
            project = VisualProject(
                project_id=f"visual_{uuid4().hex}",
                created_at=datetime.now(timezone.utc),
                created_by=actor,
                definition=definition,
                layers=layers,
                image=image,
                regions=poster.regions,
            )
            files = {"image.png": preview}
            files.update({f"layers/{name}.png": content for name, content in poster.layers.items()})
            self._persist_new(project, files)
            return project

    def _persist_new(self, project: VisualProject, files: dict[str, bytes]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.directory))
        try:
            for relative, content in files.items():
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
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

    def attach_image(self, project_id: str, raw: bytes) -> VisualProject:
        clean, image = normalize_png(raw, kind="uploaded")
        with self._lock:
            project = self.get(project_id)
            if project is None:
                raise KeyError(project_id)
            if project.image is not None:
                raise ValueError("project already has an image")
            directory = self.directory / project_id
            pending_image = directory / f".pending-image-{uuid4().hex}"
            with pending_image.open("wb") as handle:
                handle.write(clean)
                handle.flush()
                os.fsync(handle.fileno())
            pending_image.replace(directory / "image.png")
            updated = project.model_copy(update={"image": image})
            pending_manifest = directory / f".pending-manifest-{uuid4().hex}"
            try:
                with pending_manifest.open("wb") as handle:
                    handle.write(updated.model_dump_json(indent=2).encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                pending_manifest.replace(directory / "project.json")
            finally:
                pending_manifest.unlink(missing_ok=True)
            self.projects[project_id] = updated
            return updated

    def read_image(self, project_id: str) -> bytes | None:
        with self._lock:
            project = self.get(project_id)
            if project is None or project.image is None:
                return None
            try:
                content = (self.directory / project_id / "image.png").read_bytes()
            except OSError as exc:
                raise ValueError("stored visual image is missing") from exc
            if (len(content) != project.image.byte_length or len(content) > MAX_STORED_BYTES
                    or sha256(content).hexdigest() != project.image.stored_sha256):
                raise ValueError("stored visual image failed integrity check")
            return content

    def read_bundle(self, project_id: str) -> bytes | None:
        with self._lock:
            project = self.get(project_id)
            if project is None or project.image is None or project.image.kind != "demo":
                return None
            preview = self.read_image(project_id)
            if preview is None:
                return None
            output = BytesIO()
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("project.json", project.model_dump_json(indent=2))
                archive.writestr("image.png", preview)
                for layer in project.layers:
                    if layer.asset_file is None:
                        continue
                    try:
                        content = (self.directory / project_id / layer.asset_file).read_bytes()
                    except OSError as exc:
                        raise ValueError("stored visual layer is missing") from exc
                    if (len(content) > MAX_STORED_BYTES or layer.asset_sha256 is None
                            or sha256(content).hexdigest() != layer.asset_sha256):
                        raise ValueError("stored visual layer failed integrity check")
                    archive.writestr(layer.asset_file, content)
            return output.getvalue()

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
                    "has_image": project.image is not None,
                    "image_kind": project.image.kind if project.image else None,
                    "created_at": project.created_at.isoformat(),
                })
            return {"items": items, "count": len(items)}
