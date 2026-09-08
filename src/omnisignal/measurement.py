"""Versioned, deterministic query/entity definitions for repeatable observations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml


Identifier = str
AccessTier = Literal["official_api", "anonymous_public", "authorized_experimental"]


def _fingerprint(document: object) -> str:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class VersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: int = Field(ge=1, le=1_000_000, strict=True)


class KeywordSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: int = Field(ge=1, le=1_000_000, strict=True)
    display_name: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=300)
    is_example: bool
    queries: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("queries")
    @classmethod
    def valid_queries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 120 or any(ord(char) < 32 for char in value) for value in cleaned):
            raise ValueError("queries must contain 1-120 safe characters")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("queries must be unique within a keyword set")
        return cleaned


class EntityDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{2,39}$")
    name: str = Field(min_length=1, max_length=100)
    role: Literal["owned", "competitor", "reference"]
    aliases: tuple[str, ...] = Field(min_length=1, max_length=30)
    official_accounts: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    domains: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("aliases")
    @classmethod
    def valid_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 100 or any(ord(char) < 32 for char in value) for value in cleaned):
            raise ValueError("entity aliases must contain 1-100 safe characters")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("entity aliases must be unique")
        return cleaned

    @field_validator("official_accounts")
    @classmethod
    def valid_accounts(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        if len(value) > 12:
            raise ValueError("too many account platforms")
        for platform, accounts in value.items():
            if not platform or len(platform) > 30 or len(accounts) > 30:
                raise ValueError("official account mapping is too large")
            if any(not account or len(account) > 160 for account in accounts):
                raise ValueError("official account id is invalid")
            if len(set(accounts)) != len(accounts):
                raise ValueError("official account ids must be unique")
        return value

    @field_validator("domains")
    @classmethod
    def valid_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip().lower() for value in values)
        if any(not value or len(value) > 253 or "/" in value or ":" in value for value in cleaned):
            raise ValueError("domains must be host names without a scheme or path")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("domains must be unique")
        return cleaned


class EntitySet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: int = Field(ge=1, le=1_000_000, strict=True)
    display_name: str = Field(min_length=3, max_length=100)
    description: str = Field(min_length=10, max_length=300)
    is_example: bool
    entities: tuple[EntityDefinition, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_entities(self) -> "EntitySet":
        if len({entity.id for entity in self.entities}) != len(self.entities):
            raise ValueError("entity ids must be unique within an entity set")
        aliases = [alias.casefold() for entity in self.entities for alias in entity.aliases]
        if len(set(aliases)) != len(aliases):
            raise ValueError("entity aliases cannot be shared within an entity set")
        return self


class KeywordSetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    keyword_sets: tuple[KeywordSet, ...] = Field(min_length=1, max_length=100)


class EntitySetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    entity_sets: tuple[EntitySet, ...] = Field(min_length=1, max_length=100)


class ObservationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    keyword_set: VersionRef
    entity_set: VersionRef | None
    access_tier: AccessTier
    scope: str = Field(min_length=10, max_length=300)
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class MeasurementCatalog:
    def __init__(self, keyword_path: Path, entity_path: Path) -> None:
        keywords = KeywordSetDocument.model_validate(yaml.safe_load(keyword_path.read_text(encoding="utf-8")))
        entities = EntitySetDocument.model_validate(yaml.safe_load(entity_path.read_text(encoding="utf-8")))
        self.keyword_sets = self._index(keywords.keyword_sets, "keyword set")
        self.entity_sets = self._index(entities.entity_sets, "entity set")

    @staticmethod
    def _index(items: tuple[KeywordSet, ...] | tuple[EntitySet, ...], label: str) -> dict[tuple[str, int], object]:
        indexed = {(item.id, item.version): item for item in items}
        if len(indexed) != len(items):
            raise RuntimeError(f"{label} id/version pairs must be unique")
        return indexed

    def keyword_set(self, reference: VersionRef) -> KeywordSet:
        item = self.keyword_sets.get((reference.id, reference.version))
        if not isinstance(item, KeywordSet):
            raise RuntimeError(f"unknown keyword set: {reference.id}@{reference.version}")
        return item

    def entity_set(self, reference: VersionRef | None) -> EntitySet | None:
        if reference is None:
            return None
        item = self.entity_sets.get((reference.id, reference.version))
        if not isinstance(item, EntitySet):
            raise RuntimeError(f"unknown entity set: {reference.id}@{reference.version}")
        return item

    def context(self, *, keyword_ref: VersionRef, entity_ref: VersionRef | None,
                access_tier: AccessTier, scope: str) -> ObservationContext:
        keyword_set = self.keyword_set(keyword_ref)
        entity_set = self.entity_set(entity_ref)
        definition = {
            "keyword_set": keyword_set.model_dump(mode="json"),
            "entity_set": entity_set.model_dump(mode="json") if entity_set else None,
            "access_tier": access_tier,
            "scope": scope,
        }
        return ObservationContext(keyword_set=keyword_ref, entity_set=entity_ref, access_tier=access_tier,
                                  scope=scope, definition_hash=_fingerprint(definition))
