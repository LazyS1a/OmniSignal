"""Bounded photo-source adapters; transport and publication are separate concerns.

Browser inputs are observations from an ordinary, authorized page session. This
module never authenticates, solves challenges, guesses original image URLs, or
grants usage rights. DOM parsers remain experimental until live acceptance.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from lxml import html


SOURCE_CONFIGS = {
    "pinterest": {"label": "Pinterest", "listing": "https://www.pinterest.com/",
        "page_hosts": ["www.pinterest.com", "pinterest.com"], "image_hosts": ["i.pinimg.com"],
        "access": "authorized_browser", "verification": "local_20_pin_sample_only",
        "description": "正常登录页面可见 Pin；已有本地样本，服务端持续采集待验收。"},
    "zcool": {"label": "站酷", "listing": "https://www.zcool.com.cn/",
        "page_hosts": ["www.zcool.com.cn", "zcool.com.cn"], "image_hosts": ["img.zcool.cn"],
        "access": "public_or_authorized_browser", "verification": "fixture_only",
        "description": "作品详情公开元数据适配；真实访问、选择器及 CDN 待验收。"},
    "huaban": {"label": "花瓣", "listing": "https://huaban.com/",
        "page_hosts": ["huaban.com", "www.huaban.com"],
        "image_hosts": ["hbimg.huabanimg.com", "gd-hbimg.huaban.com"],
        "access": "authorized_browser", "verification": "fixture_only",
        "description": "正常页面可见采集项；登录会话及真实访问待验收。"},
    "behance": {"label": "Behance", "listing": "https://www.behance.net/",
        "page_hosts": ["www.behance.net", "behance.net"],
        "image_hosts": ["mir-s3-cdn-cf.behance.net"],
        "access": "public_or_authorized_browser", "verification": "fixture_only",
        "description": "作品详情公开元数据适配；真实访问及完整项目图组待验收。"},
    "pexels": {"label": "Pexels 图片", "listing": "https://api.pexels.com/v1/search",
        "page_hosts": ["www.pexels.com", "pexels.com"], "image_hosts": ["images.pexels.com"],
        "access": "official_photo_api", "verification": "fixture_only",
        "description": "官方照片 API；需配置 API Key，未配置时不会采集视频封面。"},
}
for _spec in SOURCE_CONFIGS.values():
    _spec["hosts"] = list(dict.fromkeys(_spec["page_hosts"] + _spec["image_hosts"]))

_PATTERNS = {
    "pinterest": r"/pin/([0-9]+)/?",
    "huaban": r"/pins/([0-9]+)/?",
    "zcool": r"/work/([A-Za-z0-9_=-]+)\.html",
    "behance": r"/gallery/([0-9]+)(?:/[A-Za-z0-9_%.-]+)?/?",
    "pexels": r"/(?:[a-z]{2}-[a-z]{2}/)?photo/(?:[A-Za-z0-9_%.-]*-)?([0-9]+)/?",
}
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_PROJECT = 100


class SourceAdapterError(ValueError):
    """A safe error code; never includes response bodies or credentials."""


def source_catalog():
    return [{"id": key, **{k: list(v) if isinstance(v, list) else v for k, v in spec.items()},
             "category": "supplemental", "verified": False, "requires_credentials": True}
            for key, spec in SOURCE_CONFIGS.items()]


def _https(value, hosts, *, keep_query=False):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(x) < 32 for x in value):
        raise SourceAdapterError("invalid_source_url")
    try:
        p = urlsplit(value)
        if (p.scheme != "https" or p.hostname not in hosts or p.username or p.password
                or p.port not in (None, 443) or "\\" in value):
            raise ValueError()
    except ValueError:
        raise SourceAdapterError("outside_source_scope") from None
    return urlunsplit(("https", p.hostname, p.path, p.query if keep_query else "", ""))


def project_identity(source, url):
    if source not in SOURCE_CONFIGS:
        raise SourceAdapterError("unsupported_source")
    url = _https(url, SOURCE_CONFIGS[source]["page_hosts"])
    match = re.fullmatch(_PATTERNS[source], urlsplit(url).path)
    if not match:
        raise SourceAdapterError("outside_project_scope")
    native_id = match.group(1)
    return {"source_id": source, "source_url": url, "native_id": native_id,
            "project_id": source + "-" + hashlib.sha256(native_id.encode()).hexdigest()[:20]}


def _dimension(value):
    # A browser naturalWidth belongs only to currentSrc, not a larger srcset URL.
    return value if type(value) is int and 0 < value <= 100000 else None


def normalize_project(source, snapshot, *, basis="project-detail"):
    """Normalize an observed project; candidates fit collection's article contract.

Required snapshot: project_url,title,images[{url,width?,height?,alt?}]. Dimensions
are declarations until the collector validates downloaded image bytes. No URL
rewrites or cross-source images are permitted. Missing description stays missing.
"""
    if basis not in {"project-detail", "listing-preview", "official-photo-api"}:
        raise SourceAdapterError("invalid_evidence_basis")
    identity = project_identity(source, snapshot.get("project_url"))
    title = str(snapshot.get("title") or "").strip()[:500]
    if not title:
        raise SourceAdapterError("project_title_missing")
    images = snapshot.get("images")
    if not isinstance(images, list) or len(images) > MAX_IMAGES_PER_PROJECT:
        raise SourceAdapterError("image_count_out_of_bounds")
    candidates, rejected, seen = [], 0, set()
    for item in images:
        if not isinstance(item, dict):
            rejected += 1
            continue
        try:
            image_url = _https(item.get("url"), SOURCE_CONFIGS[source]["image_hosts"], keep_query=True)
            # These adapters ingest static photos; video, SVG and animated GIF are not photos.
            if not re.search(r"\.(?:jpe?g|png|webp|avif)$", urlsplit(image_url).path, re.I):
                raise SourceAdapterError("unsupported_image_type")
        except SourceAdapterError:
            rejected += 1
            continue
        if image_url in seen:
            continue
        seen.add(image_url)
        candidates.append({"url": image_url, "alt": str(item.get("alt") or "")[:1000],
                           "declared_width": _dimension(item.get("width")),
                           "declared_height": _dimension(item.get("height")),
                           "dimension_basis": "unverified_source_declaration", "image_basis": basis})
    if not candidates:
        raise SourceAdapterError("project_images_missing")
    return {**identity, "title": title, "text": str(snapshot.get("description") or "")[:100000],
            "author": str(snapshot.get("author") or "")[:500], "candidates": candidates,
            "rejected_count": rejected, "evidence_basis": basis,
            "usage_permissions": {key: "unknown" for key in
                                  ("ai_analysis", "internal_display", "transmission", "export", "generation")},
            "verification": SOURCE_CONFIGS[source]["verification"]}


def normalize_pinterest_sample(record):
    """Direct bridge for existing normal-login Pin probe records; preview is explicit."""
    return normalize_project("pinterest", {
        "project_url": record.get("pin_url"), "title": record.get("alt") or "Pinterest Pin",
        "images": [{"url": record.get("image_url"), "alt": record.get("alt")}],
    }, basis="listing-preview")


def _document(document):
    if not isinstance(document, (str, bytes)):
        raise SourceAdapterError("invalid_document")
    size = len(document.encode("utf-8")) if isinstance(document, str) else len(document)
    if size > MAX_HTML_BYTES:
        raise SourceAdapterError("document_too_large")
    try:
        doc = html.fromstring(document)
    except (ValueError, html.etree.ParserError):
        raise SourceAdapterError("invalid_document") from None
    # A successful HTTP response can still be an access-verification shell.
    # These two structural signatures were observed in the saved Zcool/Huaban
    # responses. Do not execute their scripts, extract challenge fields, or
    # misreport the shell as a changed project selector. Plain article text
    # mentioning a CAPTCHA must not trigger this check.
    names = {value.lower() for value in doc.xpath("//meta/@name")}
    aliyun_shell = {"aliyun_waf_aa", "aliyun_waf_bb"}.issubset(names)
    script_texts = doc.xpath("//script[not(@src)]/text()")
    edgeone_shell = (not doc.xpath("//main|//article|//a[@href]|//img[@src]")
                    and any("EO_Bot_Ssid=" in script and "__tst_status=" in script
                            for script in script_texts))
    if aliyun_shell or edgeone_shell:
        raise SourceAdapterError("access_barrier")
    return doc


def discover_projects(source, document, *, listing_url=None, limit=80):
    """Only real project anchors, never inferred IDs, JavaScript state or hidden API."""
    if source not in SOURCE_CONFIGS or source == "pexels":
        raise SourceAdapterError("browser_source_required")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise SourceAdapterError("invalid_limit")
    base = _https(listing_url or SOURCE_CONFIGS[source]["listing"], SOURCE_CONFIGS[source]["page_hosts"])
    doc, results, seen = _document(document), [], set()
    for href in doc.xpath("//a[not(ancestor::nav) and not(ancestor::header)]/@href"):
        try:
            identity = project_identity(source, urljoin(base, href))
        except SourceAdapterError:
            continue
        if identity["project_id"] not in seen:
            seen.add(identity["project_id"])
            results.append(identity["source_url"])
        if len(results) >= limit:
            break
    if not results:
        raise SourceAdapterError("listing_structure_or_access_unavailable")
    return results


def extract_project_metadata(source, document, project_url):
    """Conservative detail parser: page's own OpenGraph image only.

Full galleries require normalize_project with explicitly scoped browser images;
we never scrape all feed/main images and accidentally ingest recommendations.
"""
    identity = project_identity(source, project_url)
    doc = _document(document)
    def meta(name):
        return doc.xpath('//meta[@property=$name or @name=$name]/@content', name=name)
    canonical = meta("og:url")
    if canonical:
        actual = project_identity(source, urljoin(project_url, canonical[0]))
        if actual["project_id"] != identity["project_id"]:
            raise SourceAdapterError("project_metadata_mismatch")
    title = meta("og:title")
    description = meta("og:description") or meta("description")
    return normalize_project(source, {"project_url": project_url, "title": title[0] if title else "",
        "description": description[0] if description else "",
        "images": [{"url": urljoin(project_url, value)} for value in meta("og:image")[:MAX_IMAGES_PER_PROJECT]]})


def parse_pexels_photos(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("photos"), list) or "videos" in payload:
        raise SourceAdapterError("invalid_photo_response")
    if len(payload["photos"]) > 80:
        raise SourceAdapterError("photo_response_too_large")
    projects = []
    for item in payload["photos"]:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
            raise SourceAdapterError("invalid_photo_record")
        identity = project_identity("pexels", item.get("url"))
        if identity["native_id"] != str(item["id"]):
            raise SourceAdapterError("photo_identity_mismatch")
        if not _dimension(item.get("width")) or not _dimension(item.get("height")):
            raise SourceAdapterError("photo_dimensions_missing")
        src = item.get("src")
        projects.append(normalize_project("pexels", {
            "project_url": item["url"], "title": item.get("alt") or f"Pexels photo {item['id']}",
            "author": item.get("photographer"),
            "images": [{"url": src.get("original") if isinstance(src, dict) else None,
                        "width": item["width"], "height": item["height"], "alt": item.get("alt")}],
        }, basis="official-photo-api"))
    return projects


def fetch_pexels_photos(query, *, api_key, reserve_request, page=1, per_page=80, client=None):
    """One bounded official API call; no retries, redirects, or secret-bearing logs.

reserve_request is the caller's atomic daily request-budget reservation. Raise or
return False to refuse. Each successful reservation precedes exactly one request.
Supply httpx.Client for deterministic tests; default client is closed here.
"""
    if not isinstance(api_key, str) or not api_key.strip():
        raise SourceAdapterError("pexels_api_key_missing")
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise SourceAdapterError("invalid_photo_query")
    if type(page) is not int or not 1 <= page <= 1000 or type(per_page) is not int or not 1 <= per_page <= 80:
        raise SourceAdapterError("invalid_photo_pagination")
    if not callable(reserve_request):
        raise SourceAdapterError("request_budget_required")
    url = SOURCE_CONFIGS["pexels"]["listing"] + "?" + urlencode({"query": query.strip(), "page": page, "per_page": per_page})
    if reserve_request() is False:
        raise SourceAdapterError("request_budget_exhausted")
    owned = client is None
    client = client or httpx.Client(timeout=25, follow_redirects=False)
    try:
        with client.stream("GET", url, headers={"Authorization": api_key}, follow_redirects=False, timeout=25) as response:
            if response.status_code in (401, 403):
                raise SourceAdapterError("pexels_authorization_required")
            if response.status_code == 429:
                raise SourceAdapterError("pexels_rate_limited")
            if response.status_code != 200:
                raise SourceAdapterError("pexels_http_error")
            if "application/json" not in response.headers.get("content-type", "").lower():
                raise SourceAdapterError("pexels_unexpected_content_type")
            import json
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise SourceAdapterError("pexels_response_too_large")
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise SourceAdapterError("invalid_photo_response") from None
            return parse_pexels_photos(payload)
    except httpx.HTTPError:
        raise SourceAdapterError("pexels_network_error") from None
    finally:
        if owned:
            client.close()
