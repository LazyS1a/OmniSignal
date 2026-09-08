"""Fail-closed authentication for the local operations control plane."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


ControlRole = Literal["viewer", "operator", "admin"]
_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_BEARER = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class ControlPrincipal:
    actor: str
    role: ControlRole
    token_sha256: str


def hash_control_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_control_principals(raw: str | None = None) -> tuple[ControlPrincipal, ...]:
    configured = os.getenv("OMNISIGNAL_CONTROL_PRINCIPALS_JSON") if raw is None else raw
    if not configured:
        return ()
    try:
        document = json.loads(configured)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("control principal configuration is invalid") from exc
    if not isinstance(document, list) or not 1 <= len(document) <= 50:
        raise RuntimeError("control principal configuration must contain 1 to 50 entries")

    principals: list[ControlPrincipal] = []
    actors: set[str] = set()
    digests: set[str] = set()
    for item in document:
        if not isinstance(item, dict) or set(item) != {"actor", "role", "token_sha256"}:
            raise RuntimeError("control principal entry is invalid")
        actor = item.get("actor")
        role = item.get("role")
        digest = item.get("token_sha256")
        if not isinstance(actor, str) or _ACTOR.fullmatch(actor) is None:
            raise RuntimeError("control principal actor is invalid")
        if role not in {"viewer", "operator", "admin"}:
            raise RuntimeError("control principal role is invalid")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest.lower()) is None:
            raise RuntimeError("control principal token digest is invalid")
        digest = digest.lower()
        if actor in actors or digest in digests:
            raise RuntimeError("control principal actor or token digest is duplicated")
        actors.add(actor)
        digests.add(digest)
        principals.append(ControlPrincipal(actor=actor, role=role, token_sha256=digest))
    return tuple(principals)


def require_control_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
) -> ControlPrincipal:
    principals: tuple[ControlPrincipal, ...] = request.app.state.control_principals
    if not principals:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operations control plane is disabled",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="control authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    if not 32 <= len(token) <= 512:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid control credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    digest = hash_control_token(token)
    matched: ControlPrincipal | None = None
    for principal in principals:
        if hmac.compare_digest(digest, principal.token_sha256):
            matched = principal
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid control credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched


def require_operator(
    principal: ControlPrincipal = Depends(require_control_principal),
) -> ControlPrincipal:
    if principal.role not in {"operator", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator role required",
        )
    return principal
