"""Bounded visual-workbench API; creating a project performs no external calls."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from omnisignal.visual_workbench import VisualProjectCreate, visual_capabilities
from omnisignal.visual_workbench.image import MAX_UPLOAD_BYTES

from .auth import ControlPrincipal, require_control_principal, require_operator


router = APIRouter(prefix="/ops/visual", tags=["operations-visual"])
_PROJECT_ID = re.compile(r"visual_[a-f0-9]{32}")


def _validate_project_id(project_id: str) -> None:
    if _PROJECT_ID.fullmatch(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="visual project not found")


@router.get("/capabilities")
def get_visual_capabilities() -> dict[str, object]:
    return visual_capabilities()


@router.get("/projects")
def list_visual_projects(request: Request) -> dict[str, object]:
    return request.app.state.visual_projects.list()


@router.get("/projects/{project_id}")
def get_visual_project(project_id: str, request: Request) -> dict[str, object]:
    _validate_project_id(project_id)
    project = request.app.state.visual_projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="visual project not found")
    return project.model_dump(mode="json")


@router.get("/projects/{project_id}/image")
def get_visual_image(
    project_id: str,
    request: Request,
    _principal: ControlPrincipal = Depends(require_control_principal),
) -> Response:
    _validate_project_id(project_id)
    try:
        content = request.app.state.visual_projects.read_image(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if content is None:
        raise HTTPException(status_code=404, detail="visual image not found")
    return Response(content, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/projects/{project_id}/bundle")
def get_visual_bundle(
    project_id: str,
    request: Request,
    _principal: ControlPrincipal = Depends(require_control_principal),
) -> Response:
    _validate_project_id(project_id)
    try:
        content = request.app.state.visual_projects.read_bundle(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if content is None:
        raise HTTPException(status_code=404, detail="visual layer bundle not found")
    return Response(content, media_type="application/zip", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/projects")
def create_visual_project(
    body: VisualProjectCreate,
    request: Request,
    principal: ControlPrincipal = Depends(require_operator),
) -> dict[str, object]:
    try:
        project = request.app.state.visual_projects.create(body, actor=principal.actor)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return project.model_dump(mode="json")


@router.post("/demo-project")
def create_visual_demo(
    request: Request,
    principal: ControlPrincipal = Depends(require_operator),
) -> dict[str, object]:
    try:
        project = request.app.state.visual_projects.create_demo(actor=principal.actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return project.model_dump(mode="json")


@router.post("/projects/{project_id}/image")
async def upload_visual_image(
    project_id: str,
    request: Request,
    _principal: ControlPrincipal = Depends(require_operator),
) -> dict[str, object]:
    _validate_project_id(project_id)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/png":
        raise HTTPException(status_code=415, detail="only image/png is supported")
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="PNG exceeds 5 MB")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="PNG exceeds 5 MB")
        body.extend(chunk)
    try:
        project = request.app.state.visual_projects.attach_image(project_id, bytes(body))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="visual project not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return project.model_dump(mode="json")
