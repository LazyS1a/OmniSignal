"""Frozen configuration models for deterministic source-to-standard mapping."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_FIELD_NAME = r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$"
_STANDARD_FIELDS = {
    "canonical_url",
    "title",
    "text",
    "published_at",
    "updated_at",
    "language",
    "parent_source_record_id",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldMap(StrictModel):
    canonical_url: str | None = Field(default=None, pattern=_FIELD_NAME)
    title: str | None = Field(default=None, pattern=_FIELD_NAME)
    text: str | None = Field(default=None, pattern=_FIELD_NAME)
    published_at: str | None = Field(default=None, pattern=_FIELD_NAME)
    updated_at: str | None = Field(default=None, pattern=_FIELD_NAME)
    language: str | None = Field(default=None, pattern=_FIELD_NAME)
    parent_source_record_id: str | None = Field(default=None, pattern=_FIELD_NAME)

    @model_validator(mode="after")
    def require_content_mapping(self) -> "FieldMap":
        if self.title is None and self.text is None:
            raise ValueError("field map requires at least title or text")
        return self


class SourceProfile(StrictModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    fields: FieldMap
    required_fields: tuple[str, ...] = ("text",)
    default_language: str = Field(default="und", pattern=r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})*$|^und$")
    min_text_chars: int = Field(default=20, ge=0, le=10_000)
    max_title_chars: int = Field(default=1_000, ge=1, le=10_000)
    max_text_chars: int = Field(default=1_000_000, ge=100, le=5_000_000)

    @field_validator("required_fields")
    @classmethod
    def validate_required_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or not set(value).issubset(_STANDARD_FIELDS):
            raise ValueError("required_fields contains duplicates or unknown standard fields")
        return value


class EntityRule(StrictModel):
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    aliases: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(_match_form(alias)) < 2 or len(alias) > 160 for alias in value):
            raise ValueError("entity aliases must contain 2-160 normalized characters")
        normalized = [_match_form(alias) for alias in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("entity contains duplicate normalized aliases")
        return value


class DuplicatePolicy(StrictModel):
    simhash_distance: int = Field(default=3, ge=0, le=3)
    ngram_size: int = Field(default=3, ge=2, le=5)
    min_content_chars: int = Field(default=40, ge=1, le=10_000)


class NormalizationConfig(StrictModel):
    config_version: str = Field(pattern=r"^1\.\d+$")
    profiles: tuple[SourceProfile, ...] = Field(min_length=1)
    entities: tuple[EntityRule, ...] = ()
    duplicate_policy: DuplicatePolicy = DuplicatePolicy()

    @model_validator(mode="after")
    def validate_unique_rules(self) -> "NormalizationConfig":
        source_ids = [profile.source_id for profile in self.profiles]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("normalization profiles contain duplicate source ids")
        entity_ids = [entity.entity_id for entity in self.entities]
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("entity rules contain duplicate entity ids")
        owners: dict[str, str] = {}
        for entity in self.entities:
            for alias in entity.aliases:
                normalized = _match_form(alias)
                owner = owners.setdefault(normalized, entity.entity_id)
                if owner != entity.entity_id:
                    raise ValueError("one normalized alias cannot belong to multiple entities")
        return self

    def config_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def profile_for(self, source_id: str) -> SourceProfile | None:
        return next((profile for profile in self.profiles if profile.source_id == source_id), None)


def load_normalization_config(path: Path) -> NormalizationConfig:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return NormalizationConfig.model_validate(document)


def _match_form(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()
