"""Packaging of the World (POTW) project adapter, verified against public HTML.

Only the archive listing and a post's own ``entry-content`` media container are
supported. Navigation cards, avatars, logos, "related project" thumbnails and
sitemap-only assets are explicitly excluded so a collection run can never pull
another project's imagery into this project's record.

Verified against the live site structure on 2026-10-23 (WordPress 7.1.3 +
Elementor). Every parse error is fail-closed: the caller records the failure
and skips the project instead of importing a partial record.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from omnisignal.knowledge.design_sources import canonical_url

# Listing = the paginated archive. Page 1 is the site root; page N>1 lives at
# /packages/page/N/. Verified: 676 pages, ~33 projects each.
CONFIG = {
    "label": "Packaging of the World · 包装设计",
    "listing": "https://packagingoftheworld.com/",
    "paged": "https://packagingoftheworld.com/packages/page/{page}/",
    # The apex host is the one this edge serves without resetting; ``www``
    # redirects/aborts depending on the front door, so it is not a fetch target.
    "hosts": ["packagingoftheworld.com"],
    "selector": "article .entry-content",
    "sitemap": "https://packagingoftheworld.com/sitemap.xml",
    "crawl_delay": 60,
}

# Hosts that may appear in links and media URLs and are normalised to the apex.
_LINK_HOSTS = frozenset({"packagingoftheworld.com", "www.packagingoftheworld.com"})

# Jetpack writes image URLs through i0.wp.com/<origin>/wp-content/uploads/...
# Both the origin host and the Jetpack CDN host are legitimate project media.
_IMAGE_HOSTS = frozenset({"packagingoftheworld.com", "www.packagingoftheworld.com", "i0.wp.com", "i1.wp.com", "i2.wp.com"})
_UPLOAD_PATH = re.compile(r"/wp-content/uploads/(?:\d{4}/\d{2}/)?[A-Za-z0-9._\-]+\.(?:jpe?g|png|webp)", re.I)
_POST_PATH = re.compile(r"/(\d{4})/(\d{2})/([A-Za-z0-9\-]+)\.html")
_MAX_PROJECTS_PER_PAGE = 40
_MAX_IMAGES_PER_PROJECT = 8


def canonical_apex(url: str) -> str:
    """Normalise a POTW URL onto the apex host the crawler actually fetches."""
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() == "www.packagingoftheworld.com":
        return url.replace("://www.packagingoftheworld.com", "://packagingoftheworld.com", 1)
    return url


def is_project(url: str) -> bool:
    """True only for a real project post: /YYYY/MM/slug.html on the canonical host."""
    try:
        parsed = urlsplit(canonical_url(url))
    except ValueError:
        return False
    if parsed.hostname not in _LINK_HOSTS:
        return False
    return bool(_POST_PATH.fullmatch(parsed.path))


def discover(document: bytes | str, base: str | None = None) -> list[str]:
    """Return project URLs from a listing page, newest first, de-duplicated.

    Only anchors that resolve to a real project path are accepted. The related
    "project-card" grid reuses the same anchors, which is fine: identity is the
    canonical URL and de-duplication happens here.
    """
    base = base or CONFIG["listing"]
    page = html.fromstring(document)
    result: list[str] = []
    seen: set[str] = set()
    for anchor in page.xpath("//a[@href]"):
        href = anchor.get("href")
        if not href:
            continue
        url = urljoin(base, href)
        if not is_project(url):
            continue
        url = canonical_apex(canonical_url(url))
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= _MAX_PROJECTS_PER_PAGE:
            break
    return result


def discover_total_pages(document: bytes | str) -> int | None:
    """Highest /packages/page/N/ number advertised in the listing navigation."""
    page = html.fromstring(document)
    highest = None
    for href in page.xpath("//a/@href"):
        match = re.fullmatch(r"(?:https?://[^/]+)?/packages/page/(\d+)/?", href or "")
        if match:
            number = int(match.group(1))
            highest = number if highest is None else max(highest, number)
    return highest


def clean(node) -> str:
    return " ".join(node.text_content().split())


def _taxonomy(article) -> dict[str, list[str]]:
    """Read the WordPress hentry classes: category-*, location-*, material-*, project-type-*."""
    facts: dict[str, list[str]] = {}
    for token in (article.get("class") or "").split():
        for prefix, field in (("category-", "category"), ("location-", "location"),
                              ("material-", "material"), ("project-type-", "project_type")):
            if token.startswith(prefix) and len(token) > len(prefix):
                facts.setdefault(field, []).append(token[len(prefix):])
    return facts


def _image_candidates(root, base_url: str) -> list[dict]:
    """Project media, restricted to this post's own uploads container.

    Rejects navigation logos, author avatars (``/avatars/``), related-project
    cards and any host outside the verified set.
    """
    candidates: list[dict] = []
    seen: set[str] = set()
    for image in root.xpath(".//img"):
        raw = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
        if not raw:
            continue
        url = urljoin(base_url, raw)
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            valid = (
                parts.scheme == "https"
                and host in _IMAGE_HOSTS
                and not parts.username
                and not parts.password
                and parts.port in (None, 443)
                and "/avatars/" not in parts.path
                and bool(_UPLOAD_PATH.search(parts.path))
            )
        except ValueError:
            valid = False
        if not valid:
            continue
        # Normalise the Jetpack CDN wrapper back to the origin URL so a single
        # project never yields two candidates for the same file. The CDN query
        # (``?fit=...&ssl=1``) is a resize hint, not part of the asset identity.
        identity, _, _ = url.partition("?")
        identity = re.sub(r"^https://i\d\.wp\.com/packagingoftheworld\.com", "https://packagingoftheworld.com", identity)
        root_path = _UPLOAD_PATH.search(identity)
        key = root_path.group(0) if root_path else identity
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"url": identity, "alt": (image.get("alt") or "")[:300]})
        if len(candidates) >= _MAX_IMAGES_PER_PROJECT:
            break
    return candidates


def extract_article(document: bytes | str, url: str) -> dict:
    """Extract one project. Raises ValueError on any structural deviation."""
    if not is_project(url):
        raise ValueError("outside_project_scope")
    page = html.fromstring(document)
    articles = page.xpath("//article")
    if len(articles) != 1:
        raise ValueError("project_structure_changed")
    article = articles[0]

    headings = article.xpath(".//*[contains(@class,'entry-title')]")
    if len(headings) != 1:
        raise ValueError("project_title_missing")
    title = clean(headings[0])
    if not 1 <= len(title) <= 500:
        raise ValueError("project_title_out_of_bounds")

    content = article.xpath(".//*[contains(@class,'entry-content')]")
    if len(content) != 1:
        raise ValueError("project_structure_changed")
    root = content[0]

    paragraphs = [clean(p) for p in root.xpath(".//p")]
    # Drop boilerplate "Curator's Insight" heading and empty stubs.
    body = [p for p in paragraphs if len(p) >= 20]
    text = "\n\n".join(body)
    if not 40 <= len(text) <= 60000:
        raise ValueError("project_text_out_of_bounds")

    facts = _taxonomy(article)

    # Submit author is the only reliable credit outside the body text.
    submitted = article.xpath(".//*[contains(@class,'submitted-by')]//a/text()") or \
                article.xpath(".//*[contains(@class,'submitted')]//a/text()")
    if submitted:
        facts["submitted_by"] = [clean_node(x) for x in submitted if x and x.strip()][:1]

    published = page.xpath("//meta[@property='article:published_time']/@content")
    if not published:
        published = page.xpath("//time/@datetime")
    published_at = published[0][:10] if published and re.match(r"\d{4}-\d{2}-\d{2}", published[0]) else None

    candidates = _image_candidates(root, url)
    if not candidates:
        raise ValueError("project_images_missing")

    metadata_lines = [f"{key}: {', '.join(values)}" for key, values in sorted(facts.items())]
    full_text = text + ("\n\nSource metadata:\n" + "\n".join(metadata_lines) if metadata_lines else "")
    return {
        "title": title,
        "text": full_text[:20000],
        "candidates": candidates,
        "rejected": [],
        "source_facts": facts,
        "published_at": published_at,
        "date_basis": "article_published_time" if published else "listing_order_only",
    }


def clean_node(value) -> str:
    return " ".join(str(value).split())


def sitemap_entries(document: bytes | str) -> list[tuple[str, str]]:
    """Return ``(loc, lastmod)`` pairs from one sitemap or sitemap-index document.

    Jetpack serves plain XML with a default namespace, so locators are resolved
    by local-name rather than by a hard-coded prefix.
    """
    root = etree.fromstring(document.encode("utf-8") if isinstance(document, str) else document)
    entries: list[tuple[str, str]] = []
    for node in root.xpath("//*[local-name()='url'] | //*[local-name()='sitemap']"):
        loc = node.findtext("{*}loc") or node.findtext("loc")
        lastmod = node.findtext("{*}lastmod") or node.findtext("lastmod")
        if not loc:
            continue
        loc = loc.strip()
        if loc.startswith("http://"):
            # Jetpack emits http:// in sitemaps; the site only serves https.
            loc = "https://" + loc[len("http://"):]
        entries.append((loc, (lastmod or "").strip()))
    return entries
