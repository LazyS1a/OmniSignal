"""Explicit operator acceptance and bounded source paging, without auto-enablement."""
import json
import os
from pathlib import Path

from .source_adapters import SOURCE_CONFIGS, SourceAdapterError


def _ids(name):
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip() in SOURCE_CONFIGS}


def pexels_api_key():
    """Read a private key file (preferred) or the legacy environment value.

    Invalid explicit file configuration fails closed rather than falling back to
    another account's environment key. No exception includes the path or value.
    """
    configured = os.getenv("OMNISIGNAL_PEXELS_API_KEY_FILE", "").strip()
    if configured:
        try:
            path = Path(configured)
            if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
                raise SourceAdapterError("pexels_api_key_file_invalid")
            with path.open("rb") as stream:
                content = stream.read(4097)
            if len(content) > 4096:
                raise SourceAdapterError("pexels_api_key_file_invalid")
            value = content.decode("utf-8").strip()
        except (OSError, UnicodeError, ValueError):
            raise SourceAdapterError("pexels_api_key_file_invalid") from None
    else:
        value = os.getenv("OMNISIGNAL_PEXELS_API_KEY", "").strip()
    if len(value) > 4096 or any(char.isspace() for char in value):
        raise SourceAdapterError("pexels_api_key_file_invalid")
    return value


def source_readiness(source):
    if source not in SOURCE_CONFIGS:
        return {"verified": True, "state": "ready", "reasons": []}
    verified = source in _ids("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS")
    if not verified:
        return {"verified": False, "state": "source_not_validated", "reasons": ["此来源尚未完成运营采集验收"]}
    if source == "pinterest":
        from .browser_jobs import browser_config
        try:
            configured = browser_config(source)
        except ValueError:
            return {"verified": True, "state": "browser_config_invalid", "reasons": ["专用浏览器采集配置无效"]}
        if configured is not None:
            return {"verified": True, "state": "ready", "reasons": [], "transport": "browser", "session": "not_checked"}
    if source == "pexels":
        try:
            key = pexels_api_key()
        except SourceAdapterError:
            return {"verified": True, "state": "pexels_api_key_file_invalid", "reasons": ["Pexels 私有凭据文件配置无效，请检查绝对路径和文件大小"]}
        if not key:
            return {"verified": True, "state": "pexels_api_key_missing", "reasons": ["缺少 Pexels 官方图片 API 凭据"]}
    elif source not in _ids("OMNISIGNAL_WORKBENCH_PUBLIC_HTML_SOURCE_IDS"):
        return {"verified": True, "state": "source_transport_unavailable", "reasons": ["普通网页访问尚未确认，未配置授权浏览器采集会话"]}
    return {"verified": True, "state": "ready", "reasons": []}


class PhotoPageCursor:
    """Persist the exact API page plus index so interruption cannot skip records."""
    def __init__(self, store, plan_id, query):
        self.store, self.query = store, query
        self.key = "collection.pexels-page.v1:" + plan_id
        with store.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (self.key,)).fetchone()
        self.state = json.loads(row[0]) if row else {"query": query, "page": 1, "items": None, "index": 0, "empty_at": None}
        value = self.state
        if (not isinstance(value, dict) or set(value) != {"query", "page", "items", "index", "empty_at"}
                or value["query"] != query or type(value["page"]) is not int or not 1 <= value["page"] <= 1000
                or type(value["index"]) is not int or value["index"] < 0
                or (value["items"] is not None and (not isinstance(value["items"], list) or len(value["items"]) > 80
                    or value["index"] > len(value["items"])))):
            raise SourceAdapterError("invalid_photo_cursor")
        if value["empty_at"] is not None and (type(value["empty_at"]) not in (int, float) or value["empty_at"] < 0):
            raise SourceAdapterError("invalid_photo_cursor")
        # At exhaustion refresh the first page once per day, without looping on empty results.
        if value["empty_at"] is not None and store.clock() - value["empty_at"] >= 86400:
            self.state = {"query": query, "page": 1, "items": None, "index": 0, "empty_at": None}
            self.save()

    def save(self):
        with self.store.db() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (self.key, json.dumps(self.state, ensure_ascii=False, separators=(",", ":"))))

    def load_page(self, items):
        if self.state["items"] is not None or not isinstance(items, list) or len(items) > 80:
            raise SourceAdapterError("invalid_photo_cursor")
        self.state.update(items=items, index=0, empty_at=self.store.clock() if not items else None)
        self.save()

    def consumed(self):
        items = self.state["items"]
        if items is None or self.state["index"] >= len(items):
            raise SourceAdapterError("invalid_photo_cursor")
        self.state["index"] += 1
        self.save()

    def next_page(self):
        if self.state["items"] is None or self.state["index"] != len(self.state["items"]):
            raise SourceAdapterError("photo_page_incomplete")
        if self.state["empty_at"] is None:
            if self.state["page"] == 1000:
                self.state.update(items=[], index=0, empty_at=self.store.clock())
            else:
                self.state.update(page=self.state["page"] + 1, items=None, index=0)
            self.save()
