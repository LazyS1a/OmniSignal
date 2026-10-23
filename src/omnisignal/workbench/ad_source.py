"""Ads of Brands outdoor-campaign adapter, verified against public HTML.

Only the source's outdoor listing and campaign media container are supported.
No generated classifications, related thumbnails, branding logos or video stills.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from lxml import html

from omnisignal.knowledge.design_sources import canonical_url


CONFIG = {
    "label": "Ads of Brands · 户外广告",
    "listing": "https://adsofbrands.net/en/ads?media=4",
    "hosts": ["adsofbrands.net"],
    "selector": "#com-left .com-media .print-image / #com-right",
}


def is_project(url):
    try:
        parsed = urlsplit(canonical_url(url))
        return parsed.hostname == "adsofbrands.net" and bool(re.fullmatch(r"/en/ads/[a-zA-Z0-9-]+/[0-9]+", parsed.path))
    except ValueError:
        return False


def discover(document):
    page = html.fromstring(document)
    result = []
    for anchor in page.cssselect("a.item[href]"):
        url = urljoin(CONFIG["listing"], anchor.get("href"))
        if is_project(url):
            url = canonical_url(url)
            if url not in result:
                result.append(url)
        if len(result) >= 40:
            break
    return result


def clean(node):
    return " ".join(node.text_content().split())


def extract_article(document, url):
    if not is_project(url):
        raise ValueError("outside_ad_project_scope")
    page = html.fromstring(document)
    roots = page.cssselect("#com-right")
    media = page.cssselect("#com-left .com-media .print-image")
    if len(roots) != 1 or not media:
        raise ValueError("ad_project_structure_changed")
    root = roots[0]
    headings, descriptions = root.cssselect("h1"), root.cssselect(".description")
    if len(headings) != 1 or len(descriptions) != 1:
        raise ValueError("ad_project_text_missing")
    title, description = clean(headings[0]), clean(descriptions[0])
    if not (1 <= len(title) <= 1000 and 30 <= len(description) <= 18000):
        raise ValueError("ad_project_text_out_of_bounds")
    types = root.cssselect(".media-type a span")
    if len(types) != 1 or clean(types[0]).lower() != "outdoor":
        raise ValueError("unsupported_ad_media_type")

    # Preserve explicitly labelled source facts separately from our review state.
    facts = {"media_type": clean(types[0])}
    for name, selector in (("country", ".country a"), ("industry", ".industry a")):
        nodes = root.cssselect(selector)
        if len(nodes) == 1:
            facts[name] = clean(nodes[0])[:300]
    for credit in root.cssselect("#credits-box .credit"):
        labels = credit.cssselect(".position")
        values = credit.xpath("./a | ./span")
        if len(labels) == 1 and values:
            key = clean(labels[0]).rstrip(":").lower()
            if key in {"advertising agency", "date"}:
                facts[key.replace(" ", "_")] = "; ".join(clean(v) for v in values)[:500]
    candidates, seen = [], set()
    for container in media:
        for raw in container.xpath(".//img/@src"):
            image_url = urljoin(url, raw)
            try:
                parts = urlsplit(image_url)
                valid = (parts.scheme == "https" and parts.hostname == "adsofbrands.net"
                         and not parts.username and not parts.password and parts.port in (None, 443)
                         and not parts.query and not parts.fragment
                         and re.fullmatch(r"/images/commercial_big/[0-9]+/[0-9]+\.(?:jpeg|jpg|png|webp)", parts.path))
            except ValueError:
                valid = False
            if valid and image_url not in seen:
                seen.add(image_url)
                candidates.append({"url": image_url})
    if not candidates:
        raise ValueError("ad_project_images_missing")
    text = description + "\n\nSource metadata:\n" + "\n".join(f"{key}: {value}" for key, value in facts.items())
    return {"title": title, "text": text, "candidates": candidates[:6], "rejected": [],
            "source_facts": facts, "published_at": None, "date_basis": "source_credit_label"}
