"""Authenticated, idempotent source controls for future connector runs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnisignal.storage import AuditEvent, SourceControlCommand, SourceControlState

from .auth import ControlPrincipal, require_control_principal, require_operator
from .ops import _load_registry


router = APIRouter(prefix="/ops", tags=["operations-control"])
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}")
_SENSITIVE_REASON = re.compile(
    r"(?i)(authorization|bearer|cookie|password|private[_ -]?key|secret|token|api[_ -]?key)"
)


class SourceControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    expected_version: int = Field(ge=0)
    confirmation: str = Field(min_length=1, max_length=96)
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 3 or _SENSITIVE_REASON.search(normalized):
            raise ValueError("reason is invalid or contains sensitive-data markers")
        return normalized


@router.get("/control/whoami")
def whoami(
    principal: ControlPrincipal = Depends(require_control_principal),
) -> dict[str, str]:
    return {"actor": principal.actor, "role": principal.role}


@router.post("/sources/{source_id}/control")
def set_source_control(
    source_id: str,
    body: SourceControlRequest,
    request: Request,
    principal: ControlPrincipal = Depends(require_operator),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> dict[str, object]:
    if not 1 <= len(source_id) <= 64:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid idempotency key")

    registry = _load_registry(request.app.state.registry_path)
    matches = [entry for entry in registry["sources"] if entry.get("id") == source_id]
    if len(matches) != 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    entry = matches[0]
    registry_allowed = entry.get("status") == "allowed"
    if body.enabled and (not registry_allowed or entry.get("kill_switch") is not True):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="source registry does not permit enablement",
        )

    action = "ENABLE" if body.enabled else "DISABLE"
    if body.confirmation != f"{action} {source_id}":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirmation phrase does not match the requested action",
        )

    command_hash = _sha256(f"{principal.actor}\0{idempotency_key}")
    request_hash = _sha256(
        json.dumps(
            {
                "actor": principal.actor,
                "role": principal.role,
                "source_id": source_id,
                **body.model_dump(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    with Session(request.app.state.engine) as session:
        previous_command = session.get(SourceControlCommand, command_hash)
        if previous_command is not None:
            if previous_command.request_hash != request_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="idempotency key was already used for a different request",
                )
            return {**previous_command.response_payload, "replayed": True}

        state = session.get(SourceControlState, source_id)
        current_version = state.version if state is not None else 0
        before_enabled = registry_allowed and (state.enabled if state is not None else True)
        if current_version != body.expected_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="source control version changed; refresh before retrying",
            )

        now = datetime.now(timezone.utc)
        if state is None:
            state = SourceControlState(
                source_id=source_id,
                enabled=body.enabled,
                version=1,
                updated_by=principal.actor,
                reason=body.reason,
                updated_at=now,
            )
            session.add(state)
            next_version = 1
        else:
            result = session.execute(
                update(SourceControlState)
                .where(
                    SourceControlState.source_id == source_id,
                    SourceControlState.version == body.expected_version,
                )
                .values(
                    enabled=body.enabled,
                    version=body.expected_version + 1,
                    updated_by=principal.actor,
                    reason=body.reason,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="source control version changed; refresh before retrying",
                )
            next_version = body.expected_version + 1

        effective_enabled = registry_allowed and body.enabled
        response_payload: dict[str, object] = {
            "source_id": source_id,
            "registry_status": entry.get("status"),
            "control": {
                "effective_enabled": effective_enabled,
                "version": next_version,
                "updated_by": principal.actor,
            },
            "replayed": False,
        }
        session.add(
            SourceControlCommand(
                command_hash=command_hash,
                request_hash=request_hash,
                source_id=source_id,
                actor=principal.actor,
                response_payload=response_payload,
                created_at=now,
            )
        )
        session.add(
            AuditEvent(
                actor=principal.actor,
                action="source_control_changed",
                target=source_id,
                detail={
                    "role": principal.role,
                    "before_enabled": before_enabled,
                    "after_enabled": effective_enabled,
                    "control_version": next_version,
                    "reason_summary": body.reason,
                    "idempotency_key_sha256": _sha256(idempotency_key),
                },
                created_at=now,
            )
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="source control changed concurrently; refresh before retrying",
            ) from exc
        return response_payload


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
