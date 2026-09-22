"""Bounded visual-workbench API; creating a project performs no external calls."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status

from omnisignal.visual_workbench import VisualProjectCreate, visual_capabilities

from .auth import ControlPrincipal, require_operator


router = APIRouter(prefix="/ops/visual", tags=["operations-visual"])
_PROJECT_ID = re.compile(r"visual_[a-f0-9]{32}")


@router.get("/capabilities")
def get_visual_capabilities() -> dict[str, object]:
    return visual_capabilities()


@router.get("/projects")
def list_visual_projects(request: Request) -> dict[str, object]:
    return request.app.state.visual_projects.list()


@router.get("/projects/{project_id}")
def get_visual_project(project_id: str, request: Request) -> dict[str, object]:
    if _PROJECT_ID.fullmatch(project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="visual project not found")
    project = request.app.state.visual_projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="visual project not found")
    return project.model_dump(mode="json")


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
