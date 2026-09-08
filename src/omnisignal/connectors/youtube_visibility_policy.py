"""Bounded configuration for video-search sample visibility."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


class BrandRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    name: str = Field(min_length=1, max_length=100)
    role: Literal["owned", "competitor"]
    aliases: tuple[str, ...] = Field(min_length=1, max_length=20)
    official_channel_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("aliases")
    @classmethod
    def valid_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        values = tuple(normalized(v) for v in values)
        if any(not v or len(v) > 100 or not any(c.isalnum() for c in v) for v in values):
            raise ValueError("aliases must contain letters or numbers and be 1-100 characters")
        if len(values) != len(set(values)):
            raise ValueError("duplicate aliases")
        return values

    @field_validator("official_channel_ids")
    @classmethod
    def valid_channels(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", v) for v in values):
            raise ValueError("official channels require exact YouTube UC ids")
        return values


class YouTubeVisibilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    query: str = Field(min_length=1, max_length=120)
    top_k: int = Field(default=20, ge=1, le=50, strict=True)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(?:-[A-Za-z]{2,4})?$")
    brands: tuple[BrandRule, ...] = Field(min_length=1, max_length=10)
    is_example: bool = True

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("invalid query")
        return value

    @model_validator(mode="after")
    def unique_brands(self) -> "YouTubeVisibilityPolicy":
        if sum(b.role == "owned" for b in self.brands) != 1:
            raise ValueError("exactly one owned brand is required")
        if len({b.id for b in self.brands}) != len(self.brands):
            raise ValueError("brand ids must be unique")
        aliases = [a for b in self.brands for a in b.aliases]
        channels = [c for b in self.brands for c in b.official_channel_ids]
        if len(aliases) != len(set(aliases)) or len(channels) != len(set(channels)):
            raise ValueError("brand rules cannot share exact aliases or official channels")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
