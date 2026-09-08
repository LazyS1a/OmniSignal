"""Strict non-secret runtime options for an authorized Hook worker."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnisignal.contracts.connector import PROHIBITED_FIELD_NAMES


class AuthorizedHookPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str = Field(default="1.0", pattern=r"^1\.\d+$")
    max_output_bytes: int = Field(default=5_000_000, ge=10_000, le=50_000_000)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=10)
    runner_options: dict[str, str | int | bool] = Field(default_factory=dict, max_length=20)

    @field_validator("runner_options")
    @classmethod
    def validate_runner_options(cls, value: dict[str, str | int | bool]) -> dict[str, str | int | bool]:
        for key, item in value.items():
            lowered = key.lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", lowered):
                raise ValueError("runner option names must be simple lowercase identifiers")
            if lowered in PROHIBITED_FIELD_NAMES or any(
                marker in lowered for marker in ("token", "secret", "password", "cookie", "credential", "private_key")
            ):
                raise ValueError("runner options cannot carry credentials")
            if isinstance(item, str) and len(item) > 256:
                raise ValueError("runner option strings are limited to 256 characters")
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 4096:
            raise ValueError("runner options exceed 4 KiB")
        return value
