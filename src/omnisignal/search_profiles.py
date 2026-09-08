"""Immutable, content-addressed Web search profiles; saving never runs a search."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnisignal.connectors.searxng_policy import SearXNGPolicy
from omnisignal.measurement import EntityDefinition, EntitySet, KeywordSet, VersionRef


class ProfileProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=80)
    role: Literal["owned", "competitor"]
    aliases: tuple[str, ...] = Field(default=(), max_length=29)
    domains: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("domains")
    @classmethod
    def domain_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        hosts = []
        for value in values:
            host = value.strip().lower().encode("idna").decode("ascii")
            labels = host.split(".")
            if len(host) > 253 or len(labels) < 2 or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels
            ):
                raise ValueError("官网应是完整域名，不含协议、路径或通配符")
            hosts.append(host)
        return tuple(hosts)

    def entity(self, index: int) -> EntityDefinition:
        aliases = tuple(dict.fromkeys([self.name.strip(), *(v.strip() for v in self.aliases)]))
        return EntityDefinition(id=f"product_{index}", name=self.name.strip(), role=self.role,
                                aliases=aliases, domains=self.domains)


class SearchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(default="", max_length=80)
    queries: tuple[str, ...] = Field(min_length=1, max_length=10)
    engines: tuple[Literal["duckduckgo", "brave"], ...] = Field(min_length=1, max_length=2)
    products: tuple[ProfileProduct, ...] = Field(default=(), max_length=10)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value):
            raise ValueError("名称不能包含控制字符")
        return value.strip()

    @model_validator(mode="after")
    def validate_definitions(self) -> "SearchProfile":
        policy = self.policy()
        object.__setattr__(self, "queries", policy.queries)
        if not self.name:
            object.__setattr__(self, "name", self.queries[0][:80])
        if self.products:
            EntitySet(id="profile_entities", version=1, display_name="检索产品集",
                      description="用户显式配置的产品及竞品匹配定义", is_example=False,
                      entities=tuple(p.entity(i) for i, p in enumerate(self.products)))
        if len(self.encoded()) > 8_000:
            raise ValueError("单份配置内容过多，请减少关键词、产品别名或域名")
        return self

    def policy(self) -> SearXNGPolicy:
        return SearXNGPolicy(queries=self.queries, engines=self.engines)

    def encoded(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")

    @property
    def identifier(self) -> str:
        return "web_" + hashlib.sha256(self.encoded()).hexdigest()[:48]


class SearchProfileStore:
    """One API process owns this store. Atomic directory publish preserves old definitions."""

    def __init__(self, directory: Path, catalog) -> None:
        self.directory = directory.resolve()
        self.catalog = catalog
        self._lock = threading.RLock()
        self.profiles: dict[str, SearchProfile] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            for path in sorted(self.directory.glob("web_*")):
                if path.name in self.profiles:
                    continue
                raw = (path / "profile.json").read_bytes()
                if len(raw) > 100_000:
                    raise ValueError("profile exceeds size limit")
                profile = SearchProfile.model_validate_json(raw)
                if path.name != profile.identifier:
                    raise ValueError("profile hash mismatch")
                if (path / "policy.json").read_bytes() != self._policy_bytes(profile):
                    raise ValueError("profile policy mismatch")
                self._register(profile)

    @staticmethod
    def _policy_bytes(profile: SearchProfile) -> bytes:
        return profile.policy().model_dump_json().encode("utf-8")

    def _register(self, profile: SearchProfile) -> None:
        from omnisignal.collection_jobs import CollectionTask

        ident = profile.identifier
        ref = VersionRef(id=ident, version=1)
        keywords = KeywordSet(id=ident, version=1, display_name=f"检索：{profile.name}",
                              description="用户在总控台保存的不可变检索关键词配置", is_example=False,
                              queries=profile.queries)
        self.catalog.measurements.keyword_sets[(ident, 1)] = keywords
        if profile.products:
            self.catalog.measurements.entity_sets[(ident, 1)] = EntitySet(
                id=ident, version=1, display_name=f"产品：{profile.name}",
                description="用户在总控台显式保存的产品及竞品匹配配置", is_example=False,
                entities=tuple(p.entity(i) for i, p in enumerate(profile.products)))
        task = CollectionTask(
            id=ident, display_name=f"检索：{profile.name}"[:80],
            description="手动检索配置；保存不采集，结果仅代表选定引擎的有限样本。",
            connector="searxng_results", source_id="searxng_results", policy="generated/profile.json",
            keyword_set=ref, entity_set=ref if profile.products else None,
            access_tier="anonymous_public", observation_scope="User configured per-engine Web search sample; no search volume or global rank.",
            is_example=False, enabled=True)
        self.catalog.profile_policies[ident] = self.directory / ident / "policy.json"
        self.catalog.tasks = {**self.catalog.tasks, ident: task}
        self.profiles = {**self.profiles, ident: profile}

    def save(self, profile: SearchProfile) -> dict[str, object]:
        with self._lock:
            self.reload()
            ident = profile.identifier
            replayed = ident in self.profiles
            if not replayed:
                if len(self.profiles) >= 100:
                    raise ValueError("最多保存 100 份配置；请先整理已有配置")
                self.directory.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.directory))
                try:
                    for name, content in (("profile.json", profile.encoded()),
                                          ("policy.json", self._policy_bytes(profile))):
                        with (staging / name).open("wb") as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                    staging.rename(self.directory / ident)
                finally:
                    # Only remove this call's private staging directory, never a published profile.
                    if staging.exists():
                        shutil.rmtree(staging)
                self._register(profile)
            return {"task_id": ident, "profile": profile.model_dump(mode="json"), "replayed": replayed}

    def list(self) -> dict[str, object]:
        with self._lock:
            self.reload()
            items = [{"task_id": key, "profile": value.model_dump(mode="json")}
                     for key, value in self.profiles.items()]
            return {"items": items, "count": len(items)}
