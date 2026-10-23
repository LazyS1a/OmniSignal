"""Bounded daily acquisition into the editorial inbox; never approves or publishes."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid

from .design_sources import SOURCES, canonical_url, discover, extract_article
from .curation import inspect_image
from .preparation import PreparationStore
from .release import _lock

INTERVAL = 86400
MAX_CASES = 200
PER_SOURCE = 2
USER_AGENT = "OmniSignal/0.2 (public design-media collection)"

class FetchError(Exception):
    def __init__(self, code): self.code = code; super().__init__(code)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

class PublicFetcher:
    def __init__(self, hosts):
        self.hosts = set(hosts)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.robots = {}; self.last = {}; self.delays = {}

    def _request(self, url, cap):
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.hostname not in self.hosts or p.username or p.password or p.port not in (None, 443):
            raise FetchError("outside_host_scope")
        addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise FetchError("non_public_address")
        wait = self.delays.get(p.hostname, 10) - (time.monotonic() - self.last.get(p.hostname, 0))
        if wait > 0: time.sleep(wait)
        self.last[p.hostname] = time.monotonic()
        try:
            with self.opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=25) as response:
                chunks = []; size = 0; started = time.monotonic()
                while True:
                    chunk = response.read(min(65536, cap + 1 - size))
                    if not chunk: break
                    chunks.append(chunk); size += len(chunk)
                    if size > cap: raise FetchError("response_too_large")
                    if time.monotonic() - started > 60: raise FetchError("response_timeout")
                return b"".join(chunks), response.headers.get_content_type()
        except urllib.error.HTTPError as error:
            raise FetchError("http_" + str(error.code)) from None
        except (OSError, urllib.error.URLError):
            raise FetchError("network_error") from None

    def get(self, url, *, image=False):
        p = urllib.parse.urlsplit(url)
        if p.hostname not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                data, _ = self._request(f"https://{p.netloc}/robots.txt", 200000)
                rp.parse(data.decode("utf-8", "replace").splitlines())
                delay = rp.crawl_delay(USER_AGENT) or 10
                rate = rp.request_rate(USER_AGENT)
                if rate: delay = max(delay, rate.seconds / rate.requests)
                if delay > 300: raise FetchError("crawl_delay_exceeds_budget")
                self.delays[p.hostname] = max(10, delay)
            except FetchError as error:
                if error.code != "http_404": raise
                rp.parse([])
            self.robots[p.hostname] = rp
        if not self.robots[p.hostname].can_fetch(USER_AGENT, url): raise FetchError("robots_denied")
        data, kind = self._request(url, 5_000_000)
        if image:
            if kind not in {"image/jpeg", "image/png", "image/webp"}: raise FetchError("not_static_image")
        else:
            if kind not in {"text/html", "application/xhtml+xml"}: raise FetchError("not_html")
            lowered = data.lower()
            if any(x in lowered for x in (b"verify you are human", b"cf-chl-captcha", b"captcha-container")):
                raise FetchError("access_barrier")
        return data

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally: temp.unlink(missing_ok=True)

def utc(value): return datetime.fromtimestamp(value, timezone.utc).isoformat()

class DesignCollector:
    def __init__(self, root, preparation, *, fetcher_factory=PublicFetcher, clock=time.time):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.store = PreparationStore(preparation)
        self.factory, self.clock = fetcher_factory, clock
        with _lock(self.root):
            path = self.root / "state.json"
            if path.exists():
                state = json.loads(path.read_text(encoding="utf-8"))
                for source in state.get("sources", {}).values():
                    if source.get("status") == "running":
                        source.update(status="interrupted", errors=[{"code": "worker_interrupted"}])
                atomic_json(path, state)

    def cycle(self):
        # One writer across processes; startup/restart honours persisted due dates.
        with _lock(self.root):
            path = self.root / "state.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sources": {}}
            state.update(version=1, interval_seconds=INTERVAL, per_source_limit=PER_SOURCE, case_limit=MAX_CASES)
            now = self.clock()
            existing = self.store.list_cases()
            known = set()
            for entry in existing:
                for source in self.store.get_case(entry["case_id"])["case"]["sources"]:
                    try: known.add(canonical_url(source["url"]))
                    except ValueError: pass
            count = len(existing)
            for site, config in SOURCES.items():
                status = state["sources"].setdefault(site, {"label": config["label"], "next_run": 0, "failures": 0, "total_imported": 0})
                if now < status["next_run"]: continue
                status.update(last_started=utc(now), status="running", imported=0, skipped=0, errors=[], next_run=now+INTERVAL)
                # Persist reservation before network or imports, so crash/restart cannot loop requests.
                atomic_json(path, state)
                self._status(state, "running")
                if count >= MAX_CASES:
                    status.update(status="capacity_reached", last_finished=utc(self.clock()))
                    continue
                fetcher = self.factory(config["hosts"])
                try:
                    urls = discover(site, fetcher.get(config["listing"]))
                    if not urls: raise FetchError("listing_structure_changed")
                    if not status.get("bootstrapped"):
                        urls = [config["seed"]] + urls
                    attempts = 0; seen = set()
                    for url in urls:
                        key = canonical_url(url)
                        if key in seen: continue
                        seen.add(key)
                        if key in known:
                            status["skipped"] += 1; continue
                        if attempts >= PER_SOURCE or count >= MAX_CASES: break
                        attempts += 1
                        try:
                            self._collect(site, url, fetcher)
                            known.add(key); count += 1
                            status["imported"] += 1; status["total_imported"] += 1
                            atomic_json(path, state)
                        except (FetchError, ValueError) as error:
                            code = error.code if isinstance(error, FetchError) else "parse_or_image_error"
                            status["errors"].append({"url": url, "code": code})
                            # Access/network failures stop this source for the whole interval.
                            if isinstance(error, FetchError): break
                    status["bootstrapped"] = True
                    status["status"] = "partial" if status["errors"] else "ok"
                except (FetchError, ValueError) as error:
                    status["status"] = "failed"
                    status["errors"].append({"code": error.code if isinstance(error, FetchError) else "parse_error"})
                except Exception as error:
                    status["status"] = "failed"
                    status["errors"].append({"code": type(error).__name__})
                if status["errors"]:
                    status["failures"] += 1
                    status["next_run"] = now + min(INTERVAL * 2**min(status["failures"]-1, 3), 7*INTERVAL)
                else: status["failures"] = 0
                status["last_finished"] = utc(self.clock())
                atomic_json(path, state)
            state["case_count"] = count
            atomic_json(path, state)
            self._status(state, "waiting")
            return state

    def _status(self, state, worker):
        atomic_json(self.root / "status.json", {**state, "worker": worker, "heartbeat": utc(self.clock())})

    def _collect(self, site, url, fetcher):
        raw = fetcher.get(url)
        article = extract_article(site, raw, url)
        case_id = site + "-" + hashlib.sha256(canonical_url(url).encode()).hexdigest()[:20]
        unknown = {key: "unknown" for key in ("display", "download", "export", "generation", "analysis")}
        with tempfile.TemporaryDirectory(prefix="case-", dir=self.root) as temp:
            staging = Path(temp); (staging/"images").mkdir()
            images = []; hashes = set()
            for candidate in article["candidates"][:6]:
                if len(images) >= 3: break
                data = fetcher.get(candidate["url"], image=True)
                try: info = inspect_image(data, hashes)
                except ValueError: continue
                name = f"images/{info['sha256']}.jpg"
                (staging/name).write_bytes(data)
                images.append({"image_id": "img-"+info["sha256"][:16], "source_id": "source-1", "path": name,
                               "role": "reference", "caption_zh": "自动采集的参考图，封面与用途待整理",
                               "original_url": candidate["url"], "permissions": dict(unknown)})
            if not images: raise ValueError("no_usable_images")
            case = {"case_id": case_id, "revision": 1, "title": article["title"], "brand_or_team": "待整理",
                    "summary_zh": "已获取来源正文与参考图片，中文分析、标签和封面待整理。",
                    "tags": {}, "claims": [], "permissions": dict(unknown),
                    "ingestion": {"kind": "design_collection", "source": site, "version": "1", "html_sha256": hashlib.sha256(raw).hexdigest()},
                    "sources": [{"source_id": "source-1", "url": url, "title": article["title"],
                                 "text": article["text"], "published_at": article["published_at"],
                                 "collected_at": utc(self.clock()), "evidence_locator": SOURCES[site]["selector"],
                                 "date_basis": article["date_basis"], "permissions": dict(unknown)}], "images": images}
            # Immutable source evidence is bounded by the 200-case pilot cap.
            evidence = self.root / "evidence" / case_id
            evidence.mkdir(parents=True, exist_ok=True)
            atomic_json(evidence/"source.json", {"url":url,"sha256":hashlib.sha256(raw).hexdigest(),"article":article})
            self.store.import_case(case, staging, actor="design-collector")

def main():
    root = Path(os.getenv("OMNISIGNAL_COLLECTION_DIR", "/app/data/design-collection"))
    prep = Path(os.getenv("OMNISIGNAL_PREPARATION_DIR", "/app/data/knowledge-preparation"))
    if os.getenv("OMNISIGNAL_DESIGN_COLLECTION_ENABLED", "false") != "true":
        raise SystemExit("Design collection disabled")
    collector = DesignCollector(root, prep)
    while True:
        collector.cycle()
        if os.getenv("OMNISIGNAL_COLLECTION_ONCE") == "true": break
        time.sleep(60)

if __name__ == "__main__": main()
