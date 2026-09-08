"""Scrapy worker process for one bounded static-web collection job."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import scrapy
from lxml import html as lxml_html
from lxml_html_clean import Cleaner
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from trafilatura import bare_extraction

from .static_web_policy import hostname_is_non_public, url_is_within_prefix


_CAPTCHA_MARKERS = (
    "g-recaptcha",
    "hcaptcha",
    "cf-chl-captcha",
    "captcha-container",
    "verify you are human",
)


class NetworkBoundaryMiddleware:
    """Fail closed on every request, including DNS-resolved private addresses."""

    def __init__(self, prefixes: list[str], allow_private_network: bool) -> None:
        self.prefixes = prefixes
        self.allow_private_network = allow_private_network
        self.allowed_origins = {
            (urlparse(prefix).scheme, urlparse(prefix).hostname, urlparse(prefix).port)
            for prefix in prefixes
        }

    @classmethod
    def from_crawler(cls, crawler: Any) -> "NetworkBoundaryMiddleware":
        return cls(
            list(crawler.settings.getlist("OMNISIGNAL_ALLOWED_PREFIXES")),
            crawler.settings.getbool("OMNISIGNAL_ALLOW_PRIVATE_NETWORK"),
        )

    def process_request(self, request: scrapy.Request, spider: scrapy.Spider) -> None:
        del spider
        parsed = urlparse(request.url)
        is_robots = parsed.path == "/robots.txt" and (parsed.scheme, parsed.hostname, parsed.port) in self.allowed_origins
        if not is_robots and not any(url_is_within_prefix(request.url, prefix) for prefix in self.prefixes):
            raise IgnoreRequest("URL rejected by OmniSignal allowlist")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise IgnoreRequest("URL rejected by OmniSignal scheme gate")
        if self.allow_private_network:
            return
        if parsed.scheme != "https" or hostname_is_non_public(parsed.hostname):
            raise IgnoreRequest("URL rejected by OmniSignal public-network gate")
        try:
            addresses = {
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise IgnoreRequest("URL DNS resolution failed closed") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise IgnoreRequest("URL DNS resolved to a non-public address")


class ComplianceSpider(scrapy.Spider):
    name = "omnisignal_static_web"

    def __init__(self, *, job_path: str, result_path: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.job = json.loads(Path(job_path).read_text(encoding="utf-8"))
        self.result_path = Path(result_path)
        self.pages: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.requested_urls = list(self.job["start_urls"])
        self.allowed_domains = sorted({urlparse(url).hostname or "" for url in self.requested_urls})

    async def start(self):
        for url in self.requested_urls:
            validators = self.job.get("validators", {}).get(url, {})
            headers = {"Cache-Control": "no-cache"}
            if validators.get("etag"):
                headers["If-None-Match"] = validators["etag"]
            if validators.get("last_modified"):
                headers["If-Modified-Since"] = validators["last_modified"]
            yield scrapy.Request(
                url,
                callback=self.parse_page,
                errback=self.request_failed,
                headers=headers,
                meta={"handle_httpstatus_all": True},
            )

    def parse_page(self, response: scrapy.http.Response):
        if response.status in {401, 403}:
            self.failures.append({"url": response.url, "kind": "access_barrier", "status": response.status})
            return
        if response.status == 429:
            retry_after = response.headers.get(b"Retry-After", b"").decode("ascii", errors="ignore")
            self.failures.append(
                {"url": response.url, "kind": "rate_limit", "status": 429, "retry_after": retry_after}
            )
            return
        if response.status == 404:
            self.failures.append({"url": response.url, "kind": "not_found", "status": 404})
            return
        if 300 <= response.status <= 399:
            self.failures.append({"url": response.url, "kind": "redirect_blocked", "status": response.status})
            return
        if response.status >= 500:
            self.failures.append({"url": response.url, "kind": "upstream", "status": response.status})
            return
        if response.status != 200:
            self.failures.append({"url": response.url, "kind": "unexpected_status", "status": response.status})
            return

        content_type = response.headers.get(b"Content-Type", b"").decode("latin-1", errors="replace")
        if "text/html" not in content_type.lower() and "application/xhtml+xml" not in content_type.lower():
            self.failures.append({"url": response.url, "kind": "non_html", "status": response.status})
            return
        lowered = response.text.lower()
        if any(marker in lowered for marker in _CAPTCHA_MARKERS):
            self.failures.append({"url": response.url, "kind": "access_barrier", "status": response.status})
            return

        missing = [selector for selector in self.job["required_selectors"] if not response.css(selector)]
        if missing:
            self.failures.append(
                {"url": response.url, "kind": "schema_drift", "status": response.status, "missing": missing}
            )
            return

        document = bare_extraction(
            response.text,
            url=response.url,
            favor_precision=True,
            include_comments=False,
            include_tables=False,
            include_links=False,
            include_images=False,
            with_metadata=True,
        )
        extracted = document.as_dict() if document is not None else {}
        text = str(extracted.get("text") or "").strip()
        if len(text) < int(self.job["min_text_chars"]):
            self.failures.append(
                {"url": response.url, "kind": "schema_drift", "status": response.status, "reason": "short_text"}
            )
            return

        snapshot_selector = self.job["snapshot_selector"]
        selected_html = response.css(snapshot_selector).get()
        if selected_html is None:
            self.failures.append(
                {"url": response.url, "kind": "schema_drift", "status": response.status, "reason": "snapshot_missing"}
            )
            return
        sanitized_html = _sanitize_html(selected_html)
        self.pages.append(
            {
                "url": response.url,
                "status": response.status,
                "headers": {
                    "content-type": content_type,
                    "etag": response.headers.get(b"ETag", b"").decode("latin-1", errors="replace"),
                    "last-modified": response.headers.get(b"Last-Modified", b"").decode(
                        "latin-1", errors="replace"
                    ),
                },
                "title": str(extracted.get("title") or "").strip() or None,
                "text": text,
                "published_at": extracted.get("date"),
                "sanitized_html": sanitized_html,
                "structure_fingerprint": hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
                "cached": "cached" in response.flags,
            }
        )

    def request_failed(self, failure: Any) -> None:
        request = failure.request
        if isinstance(failure.value, IgnoreRequest):
            message = str(failure.value)
            if "robots.txt" in message:
                return
            if "OmniSignal" in message:
                self.failures.append({"url": request.url, "kind": "boundary_rejected"})
                return
        self.failures.append(
            {"url": request.url, "kind": "request_failed", "exception": type(failure.value).__name__}
        )

    def closed(self, reason: str) -> None:
        stats = self.crawler.stats.get_stats()
        if int(stats.get("robotstxt/forbidden", 0)):
            self.failures.append(
                {"kind": "robots_forbidden", "count": int(stats.get("robotstxt/forbidden", 0))}
            )
        result = {
            "worker_schema_version": "1.0",
            "reason": reason,
            "pages": self.pages,
            "failures": self.failures,
            "stats": {
                "request_count": int(stats.get("downloader/request_count", 0)),
                "response_count": int(stats.get("downloader/response_count", 0)),
                "robots_forbidden": int(stats.get("robotstxt/forbidden", 0)),
                "httpcache_hit": int(stats.get("httpcache/hit", 0)),
                "httpcache_miss": int(stats.get("httpcache/miss", 0)),
                "httpcache_firsthand": int(stats.get("httpcache/firsthand", 0)),
                "httpcache_revalidate": int(stats.get("httpcache/revalidate", 0)),
                "httpcache_store": int(stats.get("httpcache/store", 0)),
                "httpcache_uncacheable": int(stats.get("httpcache/uncacheable", 0)),
                "httpcache_invalidate": int(stats.get("httpcache/invalidate", 0)),
            },
        }
        _atomic_json_write(self.result_path, result)


def _sanitize_html(fragment: str) -> str:
    root = lxml_html.fragment_fromstring(fragment, create_parent="section")
    cleaner = Cleaner(
        scripts=True,
        javascript=True,
        comments=True,
        style=True,
        forms=True,
        embedded=True,
        frames=True,
        safe_attrs_only=True,
        safe_attrs=frozenset({"datetime"}),
    )
    cleaned = cleaner.clean_html(root)
    return lxml_html.tostring(cleaned, encoding="unicode", method="html")


def _atomic_json_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    job = json.loads(args.job.read_text(encoding="utf-8"))
    cache_dir = Path(job["cache_directory"]).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    delay = 60 / int(job["requests_per_minute"])
    settings = {
        "ROBOTSTXT_OBEY": True,
        "ROBOTSTXT_USER_AGENT": "OmniSignalBot",
        "USER_AGENT": "OmniSignalBot/0.1 (+compliance-crawler)",
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": delay,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": delay,
        "AUTOTHROTTLE_MAX_DELAY": 60.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 0.5,
        "DOWNLOAD_TIMEOUT": int(job["timeout_seconds"]),
        "DOWNLOAD_MAXSIZE": int(job["max_response_bytes"]),
        "RETRY_ENABLED": True,
        "RETRY_TIMES": max(0, int(job["max_attempts"]) - 1),
        "RETRY_HTTP_CODES": [429, 500, 502, 503, 504],
        "REDIRECT_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "COOKIES_ENABLED": False,
        "HTTPPROXY_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.RFC2616Policy",
        "HTTPCACHE_STORAGE": "scrapy.extensions.httpcache.FilesystemCacheStorage",
        "HTTPCACHE_DIR": str(cache_dir),
        "DOWNLOADER_MIDDLEWARES": {
            "omnisignal.connectors.static_web_worker.NetworkBoundaryMiddleware": 25,
        },
        "OMNISIGNAL_ALLOWED_PREFIXES": list(job["allowed_url_prefixes"]),
        "OMNISIGNAL_ALLOW_PRIVATE_NETWORK": bool(job["allow_private_network"]),
        "LOG_ENABLED": False,
    }
    process = CrawlerProcess(settings=settings)
    process.crawl(ComplianceSpider, job_path=str(args.job), result_path=str(args.result))
    process.start(stop_after_crawl=True)
    return 0 if args.result.exists() else 2


if __name__ == "__main__":
    raise SystemExit(main())
