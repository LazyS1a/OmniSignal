"""Strict configuration for anonymous public search signals."""

from __future__ import annotations

from enum import StrEnum
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchProperty(StrEnum):
    WEB = "web"
    YOUTUBE = "youtube"


class PublicSearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keywords: tuple[str, ...] = Field(min_length=1, max_length=5)
    geo: str = Field(default="US", max_length=16)
    timeframe: str = Field(default="today 3-m", min_length=1, max_length=32)
    search_property: SearchProperty = SearchProperty.YOUTUBE
    language: str = Field(default="en-US", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
    timezone_minutes: int = Field(default=0, ge=-840, le=840)
    include_suggestions: bool = True
    max_suggestions: int = Field(default=10, ge=1, le=10)
    max_output_bytes: int = Field(default=4_000_000, ge=100_000, le=10_000_000)

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 100 or "\n" in value or "\r" in value for value in cleaned):
            raise ValueError("keywords must contain 1-100 safe characters")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("keywords must be unique")
        return cleaned

    @field_validator("geo")
    @classmethod
    def validate_geo(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized and not re.fullmatch(r"[A-Z]{2}(?:-[A-Z0-9]{1,3})?", normalized):
            raise ValueError("geo must be empty, a country code, or a supported region code")
        return normalized

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9 :\-]+", normalized):
            raise ValueError("timeframe contains unsupported characters")
        return normalized
