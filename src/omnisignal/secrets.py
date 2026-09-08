"""Minimal secret resolver. Secret values never enter ConnectorSpec."""

from __future__ import annotations

import os
import re


class MissingSecretError(RuntimeError):
    pass


def resolve_secret(secret_ref: str) -> str:
    match = re.fullmatch(r"env/([A-Z][A-Z0-9_]*)", secret_ref)
    if match is None:
        raise MissingSecretError("only env/VARIABLE secret references are enabled")
    variable = match.group(1)
    value = os.getenv(variable)
    if value is None or not value.strip():
        raise MissingSecretError(f"required secret reference is unavailable: env/{variable}")
    return value
