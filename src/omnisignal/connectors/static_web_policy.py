"""Strict policy contract for the process-isolated static web connector."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StaticWebPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_urls: tuple[str, ...] = Field(min_length=1, max_length=1000)
    allowed_url_prefixes: tuple[str, ...] = Field(min_length=1, max_length=100)
    required_selectors: tuple[str, ...] = Field(default=("article",), min_length=1, max_length=20)
    min_text_chars: int = Field(default=80, ge=1, le=1_000_000)
    max_response_bytes: int = Field(default=5_000_000, ge=10_000, le=50_000_000)
    allow_private_network: bool = False

    @field_validator("start_urls", "allowed_url_prefixes")
    @classmethod
    def validate_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("URL list contains duplicates")
        for value in values:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("web policy URLs require an http(s) host")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("web policy URLs cannot contain credentials or fragments")
            if re.search(r"(?i)(?:token|secret|password|api[_-]?key)=", parsed.query):
                raise ValueError("web policy URLs cannot contain secret query parameters")
        return values

    @field_validator("required_selectors")
    @classmethod
    def validate_selectors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("required_selectors contains duplicates")
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("required selectors must contain 1-200 characters")
        return values

    @model_validator(mode="after")
    def validate_scope(self) -> "StaticWebPolicy":
        for prefix in self.allowed_url_prefixes:
            if urlparse(prefix).query:
                raise ValueError("allowed URL prefixes cannot contain query parameters")
        for url in self.start_urls:
            if not any(url_is_within_prefix(url, prefix) for prefix in self.allowed_url_prefixes):
                raise ValueError(f"start URL is outside allowed prefixes: {url}")
            parsed = urlparse(url)
            if parsed.scheme != "https" and not self.allow_private_network:
                raise ValueError("public web sources must use https")
            if not self.allow_private_network and hostname_is_non_public(parsed.hostname or ""):
                raise ValueError("public web sources cannot target non-public addresses")
        return self


def url_is_within_prefix(url: str, prefix: str) -> bool:
    candidate = urlparse(url)
    allowed = urlparse(prefix)
    candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
    allowed_port = allowed.port or (443 if allowed.scheme == "https" else 80)
    if (
        candidate.scheme.lower() != allowed.scheme.lower()
        or (candidate.hostname or "").lower() != (allowed.hostname or "").lower()
        or candidate_port != allowed_port
    ):
        return False
    allowed_path = allowed.path or "/"
    if not allowed_path.endswith("/"):
        return candidate.path == allowed_path
    return candidate.path.startswith(allowed_path)


def hostname_is_non_public(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return not address.is_global
