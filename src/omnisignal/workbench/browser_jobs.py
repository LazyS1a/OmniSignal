"""Persistent browser jobs owned by the existing workbench collector.

No browser session is copied from a user's browser, and no permissions are
granted by collecting an image. The provider and all state use the same plan,
request budget, asset store and audit actor as the HTTP collector.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from . import source_adapters

BROWSER_SOURCES = frozenset({"pinterest", "huaban", "zcool", "behance"})

CONFIG_ENV = "OMNISIGNAL_WORKBENCH_BROWSER_CONFIG"
CONFIG_FIELDS = {"profile_dir", "listing_url", "headless", "max_scrolls", "max_requests", "max_seconds", "resource_paths"}
CONTROL_ERRORS = {"paused", "budget_exhausted", "worker_stopped", "request_blocked", "source_quota_reached", "request_limit", "time_limit", "profile_busy"}


def browser_config(source):
    """Read private local configuration; never serialize a profile path to UI."""
    configured = os.getenv(CONFIG_ENV, "").strip()
    if not configured:
        return None
    try:
        path = Path(configured)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"version", "sources"} or type(data["version"]) is not int or data["version"] != 1:
            raise ValueError()
        sources = data["sources"]
        if not isinstance(sources, dict) or set(sources) - BROWSER_SOURCES:
            raise ValueError()
        profiles = set()
        configs = {}
        for source_id, spec in sources.items():
            if not isinstance(spec, dict) or set(spec) - CONFIG_FIELDS or not {"profile_dir", "listing_url"} <= set(spec):
                raise ValueError()
            if not isinstance(spec["profile_dir"], str) or not Path(spec["profile_dir"]).is_absolute():
                raise ValueError()
            profile = os.path.normcase(str(Path(spec["profile_dir"]).resolve()))
            if profile in profiles:
                raise ValueError()
            profiles.add(profile)
            if not isinstance(spec["listing_url"], str) or len(spec["listing_url"]) > 4096:
                raise ValueError()
            if "headless" in spec and type(spec["headless"]) is not bool:
                raise ValueError()
            for key, floor, ceiling in (("max_scrolls", 0, 10), ("max_requests", 1, 500), ("max_seconds", 5, 300)):
                if key in spec and (type(spec[key]) is not int or not floor <= spec[key] <= ceiling):
                    raise ValueError()
            # Each provider validates its own listing and subresource scope.
            values = dict(spec)
            values["profile_dir"] = Path(values["profile_dir"])
            if "resource_paths" in values:
                if not isinstance(values["resource_paths"], list):
                    raise ValueError()
                values["resource_paths"] = tuple(tuple(entry) for entry in values["resource_paths"])
            if source_id == "pinterest":
                from .browser_collection import BrowserCollectionConfig
                configs[source_id] = BrowserCollectionConfig(**values)
            else:
                from .project_browser import ProjectBrowserConfig
                configs[source_id] = ProjectBrowserConfig(source_id=source_id, **values)
        return configs.get(source)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError("browser_config_invalid") from None


def session_state(store, source):
    with store.db() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", ("source.session.v1:" + source,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def record_session(store, source, state, reason_code=None):
    now = datetime.fromtimestamp(store.clock(), timezone.utc)
    value = {"state": state, "checked_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=15)).isoformat() if state == "ready" else None,
             "reason_code": reason_code}
    with store.db() as db:
        db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ("source.session.v1:" + source, json.dumps(value, separators=(",", ":"))))


class BrowserJobCursor:
    """Write discovered projects before downloads; acknowledge after DB import.

If a process stops after importing but before acknowledging, hash/project
receipts make the replay idempotent. A quota stop never discards the tail.
"""
    def __init__(self, store, plan_id, source, listing_url):
        self.store, self.source = store, source
        self.key = "collection.browser.v1:" + plan_id
        with store.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (self.key,)).fetchone()
        self.state = json.loads(row[0]) if row else {"listing_url": listing_url, "pending": [],
            "resume_cursor": None, "discovered_at": None, "pagination_state": "not_checked", "held": False, "reason_code": None}
        expected = {"listing_url", "pending", "resume_cursor", "discovered_at", "pagination_state", "held", "reason_code"}
        if (not isinstance(self.state, dict) or set(self.state) != expected or self.state["listing_url"] != listing_url
                or not isinstance(self.state["pending"], list) or len(self.state["pending"]) > 200
                or type(self.state["held"]) is not bool):
            raise ValueError("browser_cursor_invalid")
        for url in self.state["pending"]:
            source_adapters.project_identity(source, url)

    def save(self):
        with self.store.db() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (self.key, json.dumps(self.state, ensure_ascii=False, separators=(",", ":"))))

    def discovered(self, result):
        urls = result.get("project_urls")
        if self.state["pending"] or not isinstance(urls, list) or len(urls) > 200:
            raise ValueError("browser_cursor_invalid")
        checked = [source_adapters.project_identity(self.source, url)["source_url"] for url in urls]
        self.state.update(pending=list(dict.fromkeys(checked)), resume_cursor=result.get("resume_cursor"),
                          discovered_at=self.store.clock(), pagination_state=result.get("pagination_state", "pagination_unverified"))
        self.save()

    def consumed(self):
        self.state["pending"].pop(0)
        self.save()

    def hold(self, code):
        self.state.update(held=True, reason_code=code)
        self.save()


def run_browser_job(store, plan, reserve, fetcher, stopping, *, provider_factory=None, force=False):
    from omnisignal.knowledge.curation import inspect_image
    from omnisignal.knowledge.design_collection import FetchError
    from .acquisition import ProjectProgress, source_budget
    from .browser_collection import PinterestBrowserProvider, BrowserCollectionError, SAFE_STATES
    from .collection import _collection_evidence, _image_description, explain

    imported, processed, provider, cursor = 0, 0, None, None
    site = plan["source_id"]
    status, code = "ok", None
    try:
        config = browser_config(site)
        if config is None:
            raise ValueError("browser_session_not_configured")
        cursor = BrowserJobCursor(store, plan["id"], site, config.listing_url)
        if cursor.state["held"] and not force:
            return {"status": "stopped", "reason": explain(cursor.state["reason_code"]), "imported": 0}
        if force and cursor.state["held"]:
            cursor.state.update(held=False, reason_code=None)
            cursor.save()
        if not cursor.state["pending"]:
            # With no observed next-page link, refresh a listing at most daily.
            # This is deliberately NOT a claim that infinite pagination works.
            if (cursor.state["discovered_at"] is not None and cursor.state["resume_cursor"] is None
                    and store.clock() - cursor.state["discovered_at"] < 86400):
                return {"status": "ok", "reason": "本轮已处理可见项目；尚未验证后续分页，保留进度等待下次检查。", "imported": 0}
        if site == "pinterest":
            default_provider = PinterestBrowserProvider
        else:
            from .project_browser import ProjectBrowserProvider
            default_provider = ProjectBrowserProvider
        provider = (provider_factory or default_provider)(config, reserve,
            getattr(fetcher, "before_browser_request", fetcher.check_access),
            image_fetcher=lambda url: fetcher.get(url, image=True))
        provider.open()
        if not cursor.state["pending"]:
            cursor.discovered(provider.discover(limit=100, resume_cursor=cursor.state["resume_cursor"]))
        raw_limit = os.getenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "2")
        limit = int(raw_limit) if raw_limit in {str(n) for n in range(1, 101)} else 2
        while cursor.state["pending"] and processed < limit:
            if stopping.is_set():
                raise FetchError("worker_stopped")
            if source_budget(store, site)["allowed_remaining"] == 0:
                raise FetchError("source_quota_reached")
            url = cursor.state["pending"][0]
            identity = source_adapters.project_identity(site, url)
            receipt = ProjectProgress(store, site, identity["project_id"])
            if receipt.complete:
                cursor.consumed()
                continue
            detail = provider.detail(url)
            # Normalize again at the boundary; provider output cannot grant rights.
            article = source_adapters.normalize_project(site, detail["snapshot"], basis="project-detail")
            if article["project_id"] != identity["project_id"]:
                raise BrowserCollectionError("identity_conflict")
            record_session(store, site, "ready")
            hashes, image_count = set(), receipt.valid_count
            planned = article["candidates"][:6]
            for position, candidate in enumerate(planned, start=1):
                if receipt.considered(candidate["url"]):
                    continue
                if image_count >= 3:
                    break
                if stopping.is_set():
                    raise FetchError("worker_stopped")
                if source_budget(store, site)["allowed_remaining"] == 0:
                    raise FetchError("source_quota_reached")
                content = provider.get_image(candidate["url"])
                try:
                    inspect_image(content, hashes)
                except ValueError:
                    receipt.record(candidate["url"], False)
                    continue
                if source_budget(store, site)["allowed_remaining"] == 0:
                    raise FetchError("source_quota_reached")
                result = store.import_asset({"title": article["title"], "brand": "",
                    "description": _image_description(article, candidate, index=position, total=len(planned)),
                    "source_url": article["source_url"], "source_image_url": candidate["url"],
                    "source_id": site, "project_id": identity["project_id"],
                    "collection_evidence": _collection_evidence(article, candidate)},
                    content, actor="collector", deduplicate_images=True)
                imported += int(result["created"])
                receipt.record(candidate["url"], True)
                image_count += 1
            if not image_count:
                raise ValueError("no_usable_images")
            receipt.finish()
            cursor.consumed()
            processed += 1
    except (FetchError, BrowserCollectionError) as error:
        code = error.code
        # Provider exception codes are bounded; unknown strings are never exposed.
        if code not in SAFE_STATES and code not in CONTROL_ERRORS and code not in {"robots_denied", "http_401", "http_403", "http_429", "network_error", "not_static_image", "response_too_large"}:
            code = "browser_collection_failed"
    except ValueError as error:
        code = str(error) if str(error) in {"browser_config_invalid", "browser_session_not_configured", "browser_cursor_invalid", "no_usable_images"} else "browser_collection_failed"
    except Exception:
        code = "browser_collection_failed"
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass
    if code:
        status = "stopped" if code in CONTROL_ERRORS else "partial" if imported else "failed"
        if code not in CONTROL_ERRORS:
            if cursor:
                cursor.hold(code)
            session = "expired" if code in {"login_required", "session_expired", "http_401"} else "blocked"
            record_session(store, site, session, code)
    reason = explain(code) if code else f"已获取 {processed} 个项目、{imported} 张新图片，进入待处理；用途权限单独记录。"
    return {"status": status, "reason": reason, "imported": imported}
