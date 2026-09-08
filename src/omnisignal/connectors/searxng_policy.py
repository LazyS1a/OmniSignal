"""Strict policy for a locally hosted SearXNG JSON endpoint."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearXNGPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = "http://127.0.0.1:8888/search"
    queries: tuple[str, ...] = Field(min_length=1, max_length=20)
    engines: tuple[str, ...] = Field(min_length=1, max_length=8)
    categories: tuple[str, ...] = Field(default=("general",), min_length=1, max_length=5)
    language: str = Field(default="en-US", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
    time_range: Literal["day", "month", "year"] | None = None
    safe_search: Literal[0, 1, 2] = 1
    max_pages: int = Field(default=1, ge=1, le=3)
    max_results_per_slice: int = Field(default=10, ge=1, le=50)
    request_timeout_seconds: int = Field(default=20, ge=16, le=30)
    max_output_bytes: int = Field(default=4_000_000, ge=100_000, le=10_000_000)

    @field_validator("queries")
    @classmethod
    def validate_queries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 120 or any(ord(char) < 32 for char in value) for value in cleaned):
            raise ValueError("queries must contain 1-120 safe characters")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("queries must be unique")
        return cleaned

    @field_validator("engines", "categories")
    @classmethod
    def validate_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip().lower() for value in values)
        if any(
            not value
            or len(value) > 50
            or any(not (char.isalnum() or char in {"_", "-", " "}) for char in value)
            for value in cleaned
        ):
            raise ValueError("engine and category names contain unsupported characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("engine and category names must be unique")
        return cleaned

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 8888
            or parsed.path != "/search"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("endpoint must be the fixed local SearXNG URL")
        return value.strip()

    @model_validator(mode="after")
    def bounded_request_count(self) -> "SearXNGPolicy":
        if len(self.queries) * len(self.engines) * self.max_pages > 48:
            raise ValueError("SearXNG request matrix is too large")
        return self

    @property
    def maximum_records(self) -> int:
        return len(self.queries) * len(self.engines) * self.max_results_per_slice
