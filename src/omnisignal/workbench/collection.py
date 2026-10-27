"""Explicit, budgeted acquisition into the workbench's human review inbox.

Creating a plan does not fetch anything. Only enabled plans are claimed, and
every outbound HTTP attempt reserves the persisted daily request allowance.
Source adapters provide design references, never automatic advertising labels.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from omnisignal.knowledge.curation import inspect_image
from omnisignal.knowledge.design_collection import FetchError, PublicFetcher, USER_AGENT
from omnisignal.knowledge.design_sources import SOURCES, canonical_url, discover, extract_article
from omnisignal.workbench import ad_source

SOURCE_CONFIGS = {**SOURCES, "adsofbrands": ad_source.CONFIG}


ERRORS = {
    "paused": "计划已暂停，当前请求收尾后停止获取。",
    "budget_exhausted": "今日请求预算已用完，已停止后续请求；预算按 UTC 日期重置。",
    "request_blocked": "计划已暂停或请求预算已用完，未发送后续请求。",
    "worker_stopped": "服务正在停止，已保留本次获取的素材。",
    "outside_host_scope": "链接超出此来源允许的范围，已停止获取。",
    "non_public_address": "来源地址未通过公网检查，已停止获取。",
    "network_error": "暂时无法连接来源，请稍后重试。",
    "response_timeout": "来源响应超时，请稍后重试。",
    "response_too_large": "来源文件超过大小限制，已停止获取。",
    "robots_denied": "来源站点不允许自动获取此页面，请改为手工导入。",
    "crawl_delay_exceeds_budget": "来源要求的请求间隔过长，暂不执行自动获取。",
    "not_static_image": "图片不是支持的静态格式，已停止获取。",
    "not_html": "来源没有返回可解析网页，请检查来源状态。",
    "access_barrier": "来源要求人工验证，已停止自动获取。",
    "listing_structure_changed": "来源列表结构已变化，暂未找到可获取的项目。",
    "unsupported_source": "此来源尚未通过自动获取验收，可先手工导入。",
    "parse_or_image_error": "部分项目的正文或图片不完整，已跳过，需检查来源适配。",
    "unexpected_error": "本次获取未完成，已有素材保留；请查看服务诊断。",
}


def explain(code):
    if code.startswith("http_"):
        status = code.removeprefix("http_")
        if status in {"401", "403", "429"}:
            return "来源拒绝访问或限制请求，已停止自动获取，请稍后检查来源。"
        return "来源返回异常状态，本次获取已停止，请稍后重试。"
    return ERRORS.get(code, ERRORS["unexpected_error"])


class _BudgetedOpener:
    """Check after DNS validation/rate waiting, immediately before HTTP I/O."""

    def __init__(self, opener, reserve):
        self.opener, self.reserve = opener, reserve

    def open(self, *args, **kwargs):
        if not self.reserve():
            raise FetchError("request_blocked")
        return self.opener.open(*args, **kwargs)


class BudgetedFetcher(PublicFetcher):
    def __init__(self, hosts, reserve):
        super().__init__(hosts)
        # PublicFetcher enforces HTTPS, hosts, public DNS, robots, response size,
        # allowed MIME types and no redirects. This wrapper also counts robots,
        # failed HTTP attempts and image requests, with no refund after failure.
        self.opener = _BudgetedOpener(self.opener, reserve)
        self.stop_event = threading.Event()

    def _request(self, url, cap):
        # Keep the parent's security checks, with interruptible host-rate waits.
        # The opener wrapper reserves budget after waiting and before HTTP I/O.
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.hostname not in self.hosts or p.username or p.password or p.port not in (None, 443):
            raise FetchError("outside_host_scope")
        addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise FetchError("non_public_address")
        wait = self.delays.get(p.hostname, 10) - (time.monotonic() - self.last.get(p.hostname, 0))
        if self.stop_event.is_set() or (wait > 0 and self.stop_event.wait(wait)):
            raise FetchError("worker_stopped")
        self.last[p.hostname] = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with self.opener.open(request, timeout=25) as response:
                chunks, size, started = [], 0, time.monotonic()
                while True:
                    chunk = response.read(min(65536, cap + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > cap:
                        raise FetchError("response_too_large")
                    if time.monotonic() - started > 60:
                        raise FetchError("response_timeout")
                return b"".join(chunks), response.headers.get_content_type()
        except urllib.error.HTTPError as error:
            raise FetchError("http_" + str(error.code)) from None
        except (OSError, urllib.error.URLError):
            raise FetchError("network_error") from None


def project_id(source_id, url):
    return source_id + "-" + hashlib.sha256(canonical_url(url).encode()).hexdigest()[:20]


class WorkbenchCollector:
    def __init__(self, store, *, fetcher_factory=BudgetedFetcher, clock=time.time):
        self.store, self.factory, self.clock = store, fetcher_factory, clock
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._queue = queue.Queue(maxsize=20)
        self._queued = set()
        self._queue_lock = threading.Lock()
        self._thread = self._heartbeat_thread = None

    def enqueue(self, plan_id):
        """Wake the single worker; never perform network work in an HTTP route."""
        with self._queue_lock:
            if plan_id in self._queued:
                return False
            try:
                self._queue.put_nowait(plan_id)
            except queue.Full:
                return False
            self._queued.add(plan_id)
        self._wake.set()
        return True

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name="workbench-collector", daemon=True)
        self._heartbeat_thread = threading.Thread(target=self._beat, name="workbench-heartbeat", daemon=True)
        self._thread.start()
        self._heartbeat_thread.start()

    def stop(self):
        self._stopping.set()
        self._wake.set()
        # An in-flight public request may finish; the next HTTP request is denied.
        for thread in (self._thread, self._heartbeat_thread):
            if thread is not None:
                # The process lock must remain held until this returns. Returning
                # early permits a replacement worker to recover a still-live run.
                thread.join()

    def _beat(self):
        while not self._stopping.is_set():
            try:
                self.store.heartbeat()
            except Exception:
                # Do not leak raw driver errors or credentials into user status.
                pass
            self._stopping.wait(30)

    def _loop(self):
        while not self._stopping.is_set():
            try:
                while not self._stopping.is_set():
                    try:
                        plan_id = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.run(plan_id, force=True)
                    finally:
                        with self._queue_lock:
                            self._queued.discard(plan_id)
                        self._queue.task_done()
                self.sweep()
            except Exception:
                # Each run records a bounded, non-sensitive failure independently.
                # A transient store failure must not kill all future scheduling.
                pass
            self._wake.wait(30)
            self._wake.clear()

    def sweep(self):
        self.store.heartbeat()
        results = []
        for plan in self.store.list_plans(raw=True):
            if self._stopping.is_set():
                break
            if plan["enabled"] and plan["next_run"] <= self.clock():
                result = self.run(plan["id"])
                if result is not None:
                    results.append(result)
        return results

    def run(self, plan_id, force=False):
        if self._stopping.is_set():
            return None
        run_id = self.store.claim_plan(plan_id, force=force)
        if run_id is None:
            return None
        imported, projects, skipped = 0, 0, 0
        status, reason = "ok", "本次检查完成，没有发现新的项目。"

        def reserve():
            if self._stopping.is_set():
                raise FetchError("worker_stopped")
            self.store.heartbeat()
            if self.store.reserve_request(plan_id, run_id):
                return True
            current = next((p for p in self.store.list_plans(raw=True) if p["id"] == plan_id), None)
            raise FetchError("paused" if not current or not current["enabled"] else "budget_exhausted")

        try:
            plan = next(p for p in self.store.list_plans(raw=True) if p["id"] == plan_id)
            site = plan["source_id"]
            if site not in SOURCE_CONFIGS:
                raise FetchError("unsupported_source")
            config = SOURCE_CONFIGS[site]
            fetcher = self.factory(config["hosts"], reserve)
            if isinstance(fetcher, BudgetedFetcher):
                fetcher.stop_event = self._stopping
            listing = fetcher.get(config["listing"])
            urls = ad_source.discover(listing) if site == "adsofbrands" else discover(site, listing)
            if not urls:
                raise FetchError("listing_structure_changed")
            known = set(self.store.known_project_ids(site))
            seen, attempts = set(), 0
            raw_project_limit = os.getenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "2")
            project_limit = int(raw_project_limit) if raw_project_limit in {str(n) for n in range(1, 11)} else 2
            attempt_limit = max(6, project_limit * 3)
            # Listing order wins. Never prepend the old demo seed to live discovery.
            for url in urls:
                identity = project_id(site, url)
                if identity in known or identity in seen:
                    continue
                seen.add(identity)
                if projects >= project_limit or attempts >= attempt_limit:
                    break
                attempts += 1
                try:
                    raw = fetcher.get(url)
                    article = ad_source.extract_article(raw, url) if site == "adsofbrands" else extract_article(site, raw, url)
                    hashes, image_count = set(), 0
                    for candidate in article["candidates"][:6]:
                        if image_count >= 3:
                            break
                        content = fetcher.get(candidate["url"], image=True)
                        try:
                            inspect_image(content, hashes)
                        except ValueError:
                            continue
                        result = self.store.import_asset({
                            "title": article["title"][:500], "brand": "",
                            "description": article["text"][:20000],
                            "source_url": canonical_url(url),
                            "source_image_url": candidate["url"],
                            "source_id": site, "project_id": identity,
                        }, content, actor="collector")
                        image_count += 1
                        imported += int(result["created"])
                    if not image_count:
                        raise ValueError("no_usable_images")
                    projects += 1
                except ValueError:
                    skipped += 1
            if skipped:
                status, reason = "partial", ERRORS["parse_or_image_error"]
            elif imported:
                reason = f"已获取 {projects} 个项目、{imported} 张图片，等待人工审核；尚未确认广告类型和用途权限。"
        except FetchError as error:
            status = "stopped" if error.code in {"paused", "budget_exhausted", "worker_stopped", "request_blocked"} else "partial" if imported else "failed"
            reason = explain(error.code)
        except ValueError:
            status, reason = "partial" if imported else "failed", ERRORS["parse_or_image_error"]
        except Exception:
            status, reason = "partial" if imported else "failed", ERRORS["unexpected_error"]
        self.store.finish_run(plan_id, run_id, status, reason, imported)
        return {"run_id": run_id, "status": status, "reason": reason, "imported": imported}
