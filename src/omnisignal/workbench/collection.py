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
from omnisignal.workbench import ad_source, potw_source
from omnisignal.workbench import source_adapters
from omnisignal.workbench.source_readiness import PhotoPageCursor, pexels_api_key, source_readiness
from omnisignal.workbench.potw_transport import POTWTransport
from omnisignal.workbench.acquisition import (ArchiveCursor, ProjectProgress, ProjectRetry,
    daily_target, progress, fair_due_plans, record_plan_turn, source_budget, source_quotas)

SOURCE_CONFIGS = {**SOURCES, "adsofbrands": ad_source.CONFIG, "potw": potw_source.CONFIG,
                  **source_adapters.SOURCE_CONFIGS}


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
    "access_barrier_unresolved": "来源的访问校验未能通过，已停止自动获取；请确认站点未更换防护。",
    "source_not_validated": "此实验来源尚未完成运营采集验收，未发送网络请求。",
    "source_transport_unavailable": "来源需要正常可访问网页或授权浏览器会话，当前尚未配置。",
    "pexels_api_key_missing": "尚未配置 Pexels 官方图片 API 凭据，未发送请求。",
    "pexels_api_key_file_invalid": "Pexels 私有凭据文件配置无效，未发送请求。",
    "pexels_authorization_required": "Pexels 图片 API 拒绝授权，请检查对应凭据。",
    "pexels_rate_limited": "Pexels 图片 API 已限流，本次停止且不自动重试。",
    "pexels_http_error": "Pexels 图片 API 返回异常，当前分页进度已保留。",
    "pexels_network_error": "Pexels 图片 API 连接失败，当前分页进度已保留。",
    "listing_structure_or_access_unavailable": "未能读取此来源的项目列表，请核对页面结构或正常登录状态。",
    "browser_config_invalid": "浏览器采集配置无效，请检查专用会话配置。",
    "browser_session_not_configured": "尚未配置此来源的专用浏览器会话。",
    "browser_cursor_invalid": "采集进度校验失败，已有图片保留，请管理员检查进度记录。",
    "browser_collection_failed": "浏览器采集未完成，进度已保留，等待检查后再继续。",
    "browser_unavailable": "采集运行环境尚未安装浏览器组件。",
    "login_required": "来源需要正常登录，采集已暂停；登录专用采集浏览器后再手动继续。",
    "session_expired": "来源登录已失效，进度已保留；重新登录后再手动继续。",
    "challenge": "来源出现人工验证，采集已暂停，等待处理。",
    "identity_conflict": "来源页与项目编号不一致，已保留进度等待核对，未自动归入其他项目。",
    "structure_unavailable": "未能读取实际项目图片，进度已保留，请检查来源页面。",
    "source_quota_reached": "此来源的今日图片配额已达到，未处理项目已保留。",
    "no_usable_images": "当前项目没有通过解码与尺寸校验的图片，待检查。",
    "request_limit": "本轮浏览器请求上限已达到，项目进度已保留。",
    "time_limit": "本轮浏览器运行时间已达到上限，项目进度已保留。",
    "policy_denied": "来源访问检查未通过，已停止采集并保留进度。",
    "access_denied": "来源拒绝访问，已停止采集；请检查正常访问状态后再继续。",
    "profile_unmanaged": "此目录不是 Omni 专用浏览器会话，未读取现有个人浏览器配置。",
    "profile_busy": "专用采集浏览器正在使用中，当前任务保留进度。",
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

    def before_browser_request(self, url):
        """Use the HTTP policy and interruptible host delay without a body GET."""
        self.check_access(url)
        host = urllib.parse.urlsplit(url).hostname
        wait = self.delays.get(host, 10) - (time.monotonic() - self.last.get(host, 0))
        if self.stop_event.is_set() or (wait > 0 and self.stop_event.wait(wait)):
            raise FetchError("worker_stopped")
        self.last[host] = time.monotonic()
        return True

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
    if source_id in source_adapters.SOURCE_CONFIGS:
        return source_adapters.project_identity(source_id, url)["project_id"]
    return source_id + "-" + hashlib.sha256(canonical_url(url).encode()).hexdigest()[:20]


def _discover(site, config, fetcher):
    """Return ``(listing_document, project_urls)`` for one source.

    POTW is incremental and does not page through the 676-page archive: one
    listing request already yields the newest ~40 projects, which is more than
    a single run is allowed to import. Known projects are filtered downstream.
    """
    if site == "potw":
        return _discover_potw(config, fetcher)
    listing = fetcher.get(config["listing"])
    if site in source_adapters.SOURCE_CONFIGS:
        return listing, source_adapters.discover_projects(site, listing)
    if site == "adsofbrands":
        return listing, ad_source.discover(listing)
    return listing, discover(site, listing)


def _discover_potw(config, fetcher):
    """Newest projects from the site root, falling back to the deepest archive page."""
    listing = fetcher.get(config["listing"])
    urls = potw_source.discover(listing, config["listing"])
    if urls:
        return listing, urls
    total = potw_source.discover_total_pages(listing)
    if total and total >= 1:
        paged = config["paged"].format(page=total)
        page = fetcher.get(paged)
        return page, potw_source.discover(page, paged)
    return listing, urls


def _extract(site, raw, url):
    if site in source_adapters.SOURCE_CONFIGS:
        return source_adapters.extract_project_metadata(site, raw, url)
    if site == "adsofbrands":
        return ad_source.extract_article(raw, url)
    if site == "potw":
        return potw_source.extract_article(raw, url)
    return extract_article(site, raw, url)


def _image_description(article, candidate, *, index=None, total=None):
    """Project body plus the per-image identity that distinguishes siblings.

    Every image of one project shares the same article, so without the image
    line a reviewer sees N identical descriptions and cannot tell which shot
    they are approving.
    """
    parts = [article["text"]]
    alt = (candidate.get("alt") or "").strip()
    if alt:
        parts.append(f"Image caption: {alt}")
    if index is not None and total:
        parts.append(f"Image {index} of {total}")
    parts.append(f"Image file: {candidate['url'].rsplit('/', 1)[-1]}")
    return "\n\n".join(parts)[:20000]


def source_fetcher(site, config, reserve, default=BudgetedFetcher):
    """Pick the transport a source needs.

    POTW sits behind an edge that resets non-browser clients and serves a JS
    cookie interstitial; it needs ``POTWTransport``. Everything else uses the
    plain budgeted fetcher. Keeping the choice here means every caller —
    production worker, scripts, tests — gets the right transport without
    threading a factory argument through.
    """
    if site == "potw":
        return POTWTransport(config["hosts"], reserve)
    return default(config["hosts"], reserve)


class WorkbenchCollector:
    def __init__(self, store, *, fetcher_factory=None, clock=time.time, pexels_client=None, browser_factory=None):
        self.store, self.clock = store, clock
        self.factory = fetcher_factory
        self.pexels_client = pexels_client
        self.browser_factory = browser_factory
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._queue = queue.Queue(maxsize=20)
        self._queued = set()
        self._queue_lock = threading.Lock()
        self._thread = self._heartbeat_thread = None

    def _build_fetcher(self, site, config, reserve):
        if self.factory is not None:
            return self.factory(config["hosts"], reserve)
        return source_fetcher(site, config, reserve)

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
        for plan in fair_due_plans(self.store, self.store.list_plans(raw=True)):
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
        target = daily_target()
        managed = bool(target) or source_quotas() is not None
        if target and progress(self.store)["remaining"] == 0:
            return None
        current = next((p for p in self.store.list_plans(raw=True) if p["id"] == plan_id), None)
        if current is None or source_budget(self.store, current["source_id"])["allowed_remaining"] == 0:
            return None
        from .browser_jobs import session_state
        # A known access failure needs an explicit operator recheck. Polling and
        # process restarts never retry a login/challenge/identity conflict.
        if not force and session_state(self.store, current["source_id"]).get("state") in {"expired", "blocked"}:
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
            record_plan_turn(self.store, plan)
            site = plan["source_id"]
            if site not in SOURCE_CONFIGS:
                raise FetchError("unsupported_source")
            readiness = source_readiness(site)
            if readiness["state"] != "ready":
                raise FetchError(readiness["state"])
            config = SOURCE_CONFIGS[site]
            if readiness.get("transport") == "browser":
                config = {**config, "hosts": [*config["hosts"], "s.pinimg.com"]}
            fetcher = self._build_fetcher(site, config, reserve)
            if isinstance(fetcher, (BudgetedFetcher, POTWTransport)):
                fetcher.stop_event = self._stopping
            if readiness.get("transport") == "browser":
                from .browser_jobs import run_browser_job
                result = run_browser_job(self.store, plan, reserve, fetcher, self._stopping,
                                         provider_factory=self.browser_factory, force=force)
                self.store.finish_run(plan_id, run_id, result["status"], result["reason"], result["imported"])
                return {"run_id": run_id, **result, "acquisition": progress(self.store)}
            if site == "pexels":
                return self._run_pexels(plan, run_id, reserve, fetcher, target)
            listing, urls = _discover(site, config, fetcher)
            if not urls:
                raise FetchError("listing_structure_changed")
            cursor = ArchiveCursor(self.store, site, config["listing"]) if managed and site not in source_adapters.SOURCE_CONFIGS else None
            if cursor:
                cursor.observe(listing, config["listing"])
            known = set(self.store.known_project_ids(site))
            seen, attempts = set(), 0
            raw_project_limit = os.getenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "2")
            project_limit = int(raw_project_limit) if raw_project_limit in {str(n) for n in range(1, 101)} else 2
            attempt_limit = max(6, project_limit * 3)
            page_url, visited_pages = None, set()
            # Always check fresh listing first, then at most three advertised
            # archive pages. HTTP allowance and crawl-delay still apply to all.
            for page_number in range(4 if managed else 1):
                bounded, unresolved = False, False
                for url in urls:
                    identity = project_id(site, url)
                    receipt = ProjectProgress(self.store, site, identity) if managed else None
                    if (receipt.complete if receipt else identity in known):
                        continue
                    retry = ProjectRetry(self.store, identity) if managed else None
                    if retry and retry.deferred:
                        unresolved = True
                        skipped += 1
                        continue
                    if identity in seen:
                        continue
                    if projects >= project_limit or attempts >= attempt_limit or source_budget(self.store, site)["allowed_remaining"] == 0:
                        bounded = True
                        break
                    seen.add(identity)
                    attempts += 1
                    phase = "article_parse"
                    try:
                        if retry and retry.state and receipt:
                            receipt.retry_images()
                        raw = fetcher.get(url)
                        article = _extract(site, raw, url)
                        phase = "asset_ingest"
                        hashes, image_count = set(), receipt.valid_count if receipt else 0
                        planned = [c for c in article["candidates"][:6]]
                        for position, candidate in enumerate(planned, start=1):
                            if receipt and receipt.considered(candidate["url"]):
                                continue
                            if image_count >= 3 or source_budget(self.store, site)["allowed_remaining"] == 0:
                                bounded = source_budget(self.store, site)["allowed_remaining"] == 0
                                break
                            content = fetcher.get(candidate["url"], image=True)
                            try:
                                inspect_image(content, hashes)
                            except ValueError:
                                if receipt:
                                    receipt.record(candidate["url"], False)
                                continue
                            if source_budget(self.store, site)["allowed_remaining"] == 0:
                                bounded = True
                                break
                            result = self.store.import_asset({
                                "title": article["title"][:500], "brand": "",
                                "description": _image_description(article, candidate, index=position, total=len(planned)),
                                "source_url": canonical_url(url), "source_image_url": candidate["url"],
                                "source_id": site, "project_id": identity,
                                **({"collection_evidence": _collection_evidence(article, candidate)}
                                   if site in source_adapters.SOURCE_CONFIGS else {}),
                            }, content, actor="collector", deduplicate_images=managed)
                            image_count += 1
                            imported += int(result["created"])
                            if receipt:
                                receipt.record(candidate["url"], True)
                        if not image_count:
                            phase = "no_usable_images"
                            raise ValueError("no_usable_images")
                        if receipt and not bounded:
                            receipt.finish()
                        if retry:
                            retry.clear()
                        projects += 1
                    except ValueError as error:
                        skipped += 1
                        unresolved = True
                        if retry:
                            reason_code = str(error) if str(error) in ProjectRetry.REASONS else phase
                            retry.fail(reason_code)
                if not cursor or bounded:
                    break
                if page_url:
                    if unresolved:
                        cursor.defer(page_url)
                    else:
                        cursor.complete(page_url)
                page_url = cursor.next(excluding=visited_pages)
                if not page_url or page_number == 3 or projects >= project_limit or attempts >= attempt_limit:
                    break
                # Only persisted, same-site pagination discovered from a page
                # may be followed. Fetcher rechecks host, robots and budgets.
                page = fetcher.get(page_url)
                visited_pages.add(page_url)
                cursor.observe(page, page_url)
                urls = (potw_source.discover(page, page_url) if site == "potw" else
                        ad_source.discover(page) if site == "adsofbrands" else discover(site, page))
                if not urls:
                    # A temporarily empty or changed archive is not evidence
                    # that its candidates were processed. Keep the cursor for
                    # a later normal run; do not silently lose this page.
                    raise FetchError("listing_structure_changed")
            if skipped:
                status, reason = "partial", ERRORS["parse_or_image_error"]
            elif imported:
                reason = f"已获取 {projects} 个项目、{imported} 张新图片，进入分类与审核流程；用途权限单独记录。"
        except FetchError as error:
            status = "stopped" if error.code in {"paused", "budget_exhausted", "worker_stopped", "request_blocked"} else "partial" if imported else "failed"
            reason = explain(error.code)
        except source_adapters.SourceAdapterError as error:
            status, reason = "partial" if imported else "failed", explain(str(error))
        except ValueError:
            status, reason = "partial" if imported else "failed", ERRORS["parse_or_image_error"]
        except Exception:
            status, reason = "partial" if imported else "failed", ERRORS["unexpected_error"]
        self.store.finish_run(plan_id, run_id, status, reason, imported)
        return {"run_id": run_id, "status": status, "reason": reason, "imported": imported,
                "acquisition": progress(self.store) if managed else None}

    def _run_pexels(self, plan, run_id, reserve, fetcher, target):
        imported, status, reason = 0, "ok", "本次检查完成，没有发现新的图片。"
        try:
            cursor = PhotoPageCursor(self.store, plan["id"], plan.get("query", ""))
            raw_limit = os.getenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "2")
            limit = int(raw_limit) if raw_limit in {str(n) for n in range(1, 101)} else 2
            considered, pages = 0, 0
            known = self.store.known_project_ids("pexels")
            while considered < limit and pages < 3:
                if self._stopping.is_set():
                    raise FetchError("worker_stopped")
                if source_budget(self.store, "pexels")["allowed_remaining"] == 0:
                    break
                if cursor.state["empty_at"] is not None:
                    break
                if cursor.state["items"] is None:
                    items = source_adapters.fetch_pexels_photos(plan.get("query", ""),
                        api_key=pexels_api_key(), reserve_request=reserve,
                        page=cursor.state["page"], per_page=80, client=self.pexels_client)
                    cursor.load_page(items)
                    pages += 1
                    if not items:
                        break
                items = cursor.state["items"]
                while cursor.state["index"] < len(items) and considered < limit:
                    if source_budget(self.store, "pexels")["allowed_remaining"] == 0:
                        break
                    article = items[cursor.state["index"]]
                    identity = project_id("pexels", article["source_url"])
                    if identity not in known:
                        candidate = article["candidates"][0]
                        content = fetcher.get(candidate["url"], image=True)
                        inspect_image(content, set())
                        if source_budget(self.store, "pexels")["allowed_remaining"] == 0:
                            break
                        result = self.store.import_asset({
                            "title": article["title"][:500], "brand": "",
                            "description": _image_description(article, candidate, index=1, total=1),
                            "source_url": article["source_url"], "source_image_url": candidate["url"],
                            "source_id": "pexels", "project_id": identity,
                            "collection_evidence": _collection_evidence(article, candidate),
                        }, content, actor="collector", deduplicate_images=True)
                        imported += int(result["created"])
                        known.add(identity)
                    cursor.consumed()
                    considered += 1
                if cursor.state["index"] == len(items):
                    cursor.next_page()
                else:
                    break
            if imported:
                reason = f"已获取 {imported} 张 Pexels 图片，进入分类流程；用途权限单独记录。"
        except FetchError as error:
            status = "stopped" if error.code in {"paused", "budget_exhausted", "worker_stopped", "request_blocked"} else "partial" if imported else "failed"
            reason = explain(error.code)
        except source_adapters.SourceAdapterError as error:
            status, reason = "partial" if imported else "failed", explain(str(error))
        except ValueError:
            status, reason = "partial" if imported else "failed", ERRORS["parse_or_image_error"]
        except Exception:
            status, reason = "partial" if imported else "failed", ERRORS["unexpected_error"]
        self.store.finish_run(plan["id"], run_id, status, reason, imported)
        return {"run_id": run_id, "status": status, "reason": reason, "imported": imported,
                "acquisition": progress(self.store) if target else None}


def _collection_evidence(article, candidate):
    return {"basis": candidate.get("image_basis", article.get("evidence_basis")),
            "verification": article.get("verification"), "native_id": article.get("native_id"),
            "declared_width": candidate.get("declared_width"), "declared_height": candidate.get("declared_height"),
            "dimension_basis": candidate.get("dimension_basis")}


def import_browser_observation(store, source, snapshot, content, *, image_index=0):
    """Import already downloaded bytes from an ordinary browser observation.

    No network or automatic plan activation occurs. Preview evidence remains
    explicit and this manual import does not pretend to be scheduled collection.
    """
    if source not in source_adapters.SOURCE_CONFIGS or source == "pexels":
        raise source_adapters.SourceAdapterError("browser_source_required")
    article = source_adapters.normalize_project(source, snapshot, basis="listing-preview")
    if type(image_index) is not int or not 0 <= image_index < len(article["candidates"]):
        raise source_adapters.SourceAdapterError("invalid_image_index")
    candidate = article["candidates"][image_index]
    inspect_image(content, set())
    return store.import_asset({
        "title": article["title"], "brand": "",
        "description": _image_description(article, candidate, index=image_index + 1, total=len(article["candidates"])),
        "source_url": article["source_url"], "source_image_url": candidate["url"],
        "source_id": source, "project_id": article["project_id"],
        "collection_evidence": _collection_evidence(article, candidate),
    }, content, actor="operator", deduplicate_images=True)
