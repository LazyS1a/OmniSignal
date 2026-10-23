"""Bounded, explainable image candidates. No network, model or approval writes.

These rules filter obvious noise; they cannot decide design relevance or license.
The caller must enforce network policy when downloading the returned URLs.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .release import _image, KnowledgeError
from PIL import Image
import io

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_NOISE = {"related", "related-content", "related-articles", "recommendations", "listing-item", "advertisement", "advert", "newsletter"}
_TRANSFORMS = {"w", "width", "h", "height", "quality", "q", "fit"}


def resource_key(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in _TRANSFORMS and not (k == "class" and re.fullmatch(r"w\d+", v))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class _Candidates(HTMLParser):
    def __init__(self, page_url, allowed_hosts):
        super().__init__(convert_charrefs=True)
        self.page_url, self.hosts = page_url, set(allowed_hosts)
        self.stack, self.candidates, self.rejected, self.seen = [], [], [], set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img" and any(t == "article" for t, _ in self.stack):
            self.add_image(attrs)
        if tag not in _VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def add_image(self, attrs):
        if len(self.candidates) + len(self.rejected) >= 100:
            return
        raw = attrs.get("src") or attrs.get("data-src") or ""
        if raw.startswith("data:") and attrs.get("data-src"):
            raw = attrs["data-src"]
        url = urljoin(self.page_url, raw)
        reason = None
        try:
            p = urlsplit(url)
            tokens = {x for _, a in self.stack for x in (a.get("class", "") + " " + a.get("id", "")).lower().split()}
            if tokens & _NOISE:
                reason = "related_or_navigation"
            elif not raw or p.scheme != "https" or p.hostname not in self.hosts or p.username or p.password or p.port not in (None, 443):
                reason = "outside_image_scope"
            elif PurePosixPath(p.path).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                reason = "unsupported_static_format"
            width, height = int(attrs.get("width", 0)), int(attrs.get("height", 0))
            if not reason and width and height and (min(width, height) < 320 or max(width, height) > min(width, height) * 4):
                reason = "small_or_banner"
            key = resource_key(url)
            if not reason and key in self.seen:
                reason = "same_image_variant"
        except (ValueError, TypeError):
            reason = "invalid_image_metadata"
        if reason:
            self.rejected.append({"url": url[:8192], "reason": reason})
            return
        self.seen.add(key)
        self.candidates.append({"url": url, "resource_key": key, "alt": attrs.get("alt", "")[:1000],
                                "declared_width": width, "declared_height": height,
                                "selection_status": "candidate_needs_visual_review"})


def image_candidates(document: str, page_url: str, *, allowed_hosts: list[str]) -> dict:
    if len(document) > 5_000_000 or not allowed_hosts:
        raise ValueError("bounded HTML and explicit image hosts required")
    parser = _Candidates(page_url, allowed_hosts)
    parser.feed(document)
    return {"candidates": parser.candidates, "rejected": parser.rejected,
            "rules_version": "design-candidates-1", "semantic_selection": "manual_review_required"}


def inspect_image(data: bytes, seen_hashes: set[str]) -> dict:
    """Decode before accepting; byte dedup is deliberately not semantic dedup."""
    _image(data)
    sha = hashlib.sha256(data).hexdigest()
    if sha in seen_hashes:
        raise KnowledgeError("duplicate_image_bytes")
    with Image.open(io.BytesIO(data)) as img:
        w, h, fmt = img.width, img.height, img.format
    if min(w, h) < 320 or max(w, h) > min(w, h) * 4:
        raise KnowledgeError("small_or_banner")
    seen_hashes.add(sha)
    return {"sha256": sha, "width": w, "height": h, "format": fmt, "size": len(data)}
