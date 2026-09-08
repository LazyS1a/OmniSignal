"""Bounded dependency checks for allowlisted collection tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import threading
import time
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
import yaml

from omnisignal.collection_jobs import CollectionTask, CollectionTaskCatalog
from omnisignal.connectors.searxng_policy import SearXNGPolicy


ReadinessStatus = Literal["ready", "unavailable", "disabled"]


@dataclass(frozen=True, slots=True)
class TaskReadiness:
    status: ReadinessStatus
    available: bool
    detail_code: str
    message: str
    checked_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "available": self.available,
            "detail_code": self.detail_code,
            "message": self.message,
            "checked_at": self.checked_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }


class CollectionReadinessService:
    """Cache display checks, while allowing callers to force a fresh preflight."""

    def __init__(
        self,
        catalog: CollectionTaskCatalog,
        *,
        timeout_seconds: float = 2.0,
        cache_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.catalog = catalog
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.transport = transport
        self._cache: dict[str, tuple[float, TaskReadiness]] = {}
        self._lock = threading.Lock()

    def check(self, task: CollectionTask, *, force: bool = False) -> TaskReadiness:
        now = time.monotonic()
        cache_key = f"{task.id}:{task.enabled}"
        if task.enabled and task.connector == "searxng_results":
            try:
                policy = self._load_searxng_policy(task)
                cache_key = json.dumps([policy.endpoint, sorted(policy.engines)])
            except (ValueError, TypeError, yaml.YAMLError, OSError):
                pass  # The uncached check returns the safe failure state.
        with self._lock:
            cached = self._cache.get(cache_key)
            if not force and cached is not None and now - cached[0] < self.cache_seconds:
                return cached[1]
        result = self._check_uncached(task)
        with self._lock:
            self._cache[cache_key] = (now, result)
        return result

    def _check_uncached(self, task: CollectionTask) -> TaskReadiness:
        checked_at = datetime.now(timezone.utc)
        if not task.enabled:
            return TaskReadiness("disabled", False, "task_disabled", "任务配置已停用。", checked_at)
        if task.connector != "searxng_results":
            return TaskReadiness("ready", True, "configured", "任务配置已就绪，依赖会在运行时检查。", checked_at)

        try:
            policy = self._load_searxng_policy(task)
            config_url = self._config_url(policy.endpoint)
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                with client.stream("GET", config_url, headers={"Accept": "application/json"}) as response:
                    if response.status_code != 200:
                        raise ValueError("invalid readiness response")
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=16_384):
                        content.extend(chunk)
                        if len(content) > 512_000:
                            raise ValueError("readiness response too large")
            document = json.loads(content)
            if not isinstance(document, dict) or not isinstance(document.get("version"), str):
                raise ValueError("invalid readiness schema")
            engines = document.get("engines")
            if not isinstance(engines, list):
                raise ValueError("invalid engine catalog")
            configured = {
                str(item.get("name", "")).casefold()
                for item in engines
                if isinstance(item, dict) and item.get("enabled") is True
            }
            missing = sorted(engine for engine in policy.engines if engine.casefold() not in configured)
            if missing:
                return TaskReadiness(
                    "unavailable",
                    False,
                    "engine_unavailable",
                    "SearXNG 已启动，但任务所需搜索引擎未启用。",
                    checked_at,
                )
        except (httpx.HTTPError, ValueError, TypeError, ValidationError, yaml.YAMLError, OSError):
            return TaskReadiness(
                "unavailable",
                False,
                "dependency_unavailable",
                "本机 SearXNG 不可用，请先启动 Docker Desktop 和 SearXNG。",
                checked_at,
            )
        return TaskReadiness("ready", True, "searxng_ready", "本机 SearXNG 已就绪。", checked_at)

    def _load_searxng_policy(self, task: CollectionTask) -> SearXNGPolicy:
        path: Path = self.catalog.resolve_policy(task)
        return SearXNGPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    @staticmethod
    def _config_url(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        return urlunsplit((parsed.scheme, parsed.netloc, "/config", "", ""))
