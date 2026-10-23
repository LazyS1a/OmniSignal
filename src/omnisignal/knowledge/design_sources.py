"""Explicit public design-media adapters. Parsing only; no model or network."""
from datetime import datetime
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from lxml import html
from .curation import image_candidates

SOURCES = {
    "bpando": {"label": "BP&O", "listing": "https://bpando.org/", "hosts": ["bpando.org"],
               "selector": ".article-content", "seed": "https://bpando.org/2018/11/09/branding-tea-and-glory/"},
    "itsnicethat": {"label": "It’s Nice That", "listing": "https://www.itsnicethat.com/graphic-design",
                   "hosts": ["www.itsnicethat.com", "m.itsnicethat.com"], "selector": "article",
                   "seed": "https://www.itsnicethat.com/articles/whynotdesign-the-tea-tips-project-graphic-design-060223"},
}

def canonical_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        raise ValueError("public_https_required")
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))

def is_article(site, url):
    try:
        p = urlsplit(canonical_url(url))
        if site == "bpando":
            return p.hostname == "bpando.org" and bool(re.fullmatch(r"/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9-]+", p.path))
        return p.hostname == "www.itsnicethat.com" and bool(re.fullmatch(r"/articles/[a-zA-Z0-9-]+", p.path))
    except ValueError:
        return False

def discover(site, document):
    doc = html.fromstring(document)
    urls = []
    for href in doc.xpath("//a/@href"):
        url = urljoin(SOURCES[site]["listing"], href)
        if is_article(site, url):
            url = canonical_url(url)
            if site == "bpando": url += "/"
            if url not in urls: urls.append(url)
        if len(urls) >= 40: break
    return urls

def extract_article(site, document, url):
    if not is_article(site, url): raise ValueError("outside_article_scope")
    doc = html.fromstring(document)
    roots = doc.cssselect(SOURCES[site]["selector"])
    if not roots: raise ValueError("article_structure_changed")
    root = roots[0]
    headings = root.xpath(".//h1")
    if not headings: raise ValueError("article_title_missing")
    title = " ".join(headings[0].text_content().split())[:1000]
    if site == "bpando":
        paragraphs = root.cssselect(".wysiwyg--post p")
        dates = doc.xpath('//meta[@property="article:published_time"]/@content')
        date = datetime.fromisoformat(dates[0].replace("Z", "+00:00")).date().isoformat() if dates else None
    else:
        paragraphs = root.cssselect(".module-text .rich-text p")
        dates = root.xpath('.//*[@id="article-date"]//text()')
        date = datetime.strptime(" ".join(dates).strip(), "%d %B %Y").date().isoformat() if dates else None
    lines = [" ".join(p.text_content().split()) for p in paragraphs]
    text = "\n\n".join(x for x in lines if len(x) >= 30 and not re.search(r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b", x))
    if not 80 <= len(text) <= 100000: raise ValueError("article_text_out_of_bounds")
    # Normalise only the adapter's verified content container, never the whole page.
    root.tag = "article"
    candidates = image_candidates(html.tostring(root, encoding="unicode"), url, allowed_hosts=SOURCES[site]["hosts"])
    return {"title": title, "text": text, "published_at": date,
            "date_basis": "article_metadata" if site == "bpando" else "visible_article_date",
            "candidates": candidates["candidates"], "rejected": candidates["rejected"]}
