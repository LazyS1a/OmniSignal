"""Bounded daily acquisition policy and same-source, persistent archive discovery."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
import math
import os
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qs

from lxml import html


def daily_target():
    raw = os.getenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "0")
    if not raw.isdigit() or not 0 <= int(raw) <= 2000:
        raise ValueError("invalid_collection_daily_target")
    return int(raw)


def progress(store):
    # This is new distinct image content acquired by the collector, not HTTP
    # requests, manual uploads, analysis attempts, or newly published web pages.
    local = datetime.fromtimestamp(store.clock(), timezone(timedelta(hours=8)))
    start = datetime.combine(local.date(), time(), local.tzinfo).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with store.db() as db:
        rows = db.execute("""SELECT json_extract(a.document,'$.sha256') AS sha,
            json_extract(a.document,'$.source_id') AS source FROM assets a
            WHERE julianday(a.created_at)>=julianday(?) AND julianday(a.created_at)<julianday(?)
            AND EXISTS(SELECT 1 FROM audit x WHERE x.entity=a.id AND x.actor='collector' AND x.action='import')
            """, (start.isoformat(), end.isoformat())).fetchall()
    per_source = {}
    for row in rows:
        per_source.setdefault(row["source"], set()).add(row["sha"])
    actual = len({row["sha"] for row in rows})
    target = daily_target()
    return {"date": local.date().isoformat(), "timezone": "Asia/Shanghai", "target": target,
            "new_unique_images": actual, "remaining": max(0, target-actual) if target else None,
            "by_source": {key: len(values) for key, values in per_source.items()},
            "basis": "new_to_library_distinct_content", "newly_published_on_source": None}


def listing_links(site, document, base):
    """Accept only pagination links actually advertised in the fetched HTML."""
    page = html.fromstring(document)
    links = []
    expected_host = urlsplit(base).hostname
    for anchor in page.xpath("//a[@href]"):
        try:
            url = urljoin(base, anchor.get("href"))
            p = urlsplit(url)
            safe = p.scheme == "https" and p.hostname == expected_host and not p.username and not p.password and p.port in (None, 443)
        except ValueError:
            safe = False
        if not safe:
            continue
        allowed = False
        if site == "potw":
            allowed = bool(re.fullmatch(r"/packages/page/[1-9][0-9]{0,4}/?", p.path)) and not p.query
        elif site == "bpando":
            allowed = bool(re.fullmatch(r"/page/[1-9][0-9]{0,4}/?", p.path)) and not p.query
        elif site == "adsofbrands" and p.path == "/en/ads":
            query = parse_qs(p.query)
            allowed = set(query) <= {"media", "page"} and query.get("media") == ["4"] and len(query.get("page", [])) == 1 and bool(re.fullmatch(r"[1-9][0-9]{0,4}", query["page"][0]))
        elif site == "itsnicethat" and p.path == "/graphic-design":
            query = parse_qs(p.query)
            allowed = set(query) == {"page"} and len(query["page"]) == 1 and bool(re.fullmatch(r"[1-9][0-9]{0,4}", query["page"][0]))
        if allowed:
            clean = urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
            if clean not in links:
                links.append(clean)
    return links[:100]


class ArchiveCursor:
    """Advance a page only after all its candidates have been considered.

    Root is checked on every run for fresh projects. Interrupted or bounded
    runs retain the current archive page; normal per-project dedup skips work.
    """
    def __init__(self, store, site, base):
        self.store, self.site, self.base = store, site, base
        self.key = "collection.archive.v1:" + site
        with store.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (self.key,)).fetchone()
        self.state = json.loads(row[0]) if row else {"pending": [], "done": [], "checked_at": {}}
        if not isinstance(self.state, dict) or set(self.state) not in ({"pending", "done"}, {"pending", "done", "checked_at"}) or any(not isinstance(self.state[k], list) or len(self.state[k]) > 2000 or any(not isinstance(x,str) for x in self.state[k]) for k in ("pending", "done")):
            raise ValueError("invalid_archive_cursor")
        # Existing cursors migrate without forgetting progress or replaying all
        # completed pages at once. Every completed page becomes due in a week.
        self.state.setdefault("checked_at", {url: self.store.clock() for url in self.state["done"]})
        checked = self.state["checked_at"]
        if not isinstance(checked, dict) or len(checked) > 2000 or set(checked) - set(self.state["done"]) or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in checked.values()):
            raise ValueError("invalid_archive_cursor")
        self._refreshed = False

    def save(self):
        with self.store.db() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (self.key, json.dumps(self.state, separators=(",", ":"))))

    def observe(self, document, base):
        advertised = listing_links(self.site, document, base)
        for url in advertised:
            if url not in self.state["done"] and url not in self.state["pending"] and len(self.state["pending"])+len(self.state["done"]) < 2000:
                self.state["pending"].append(url)
        # Numeric pages move when new articles arrive. Reconsider at most one
        # completed page per run: root-advertised pages after a day, other pages
        # after a week, oldest first. Retain the first pending page (which might
        # contain an interrupted project) ahead of this bounded refresh.
        if base == self.base and not self._refreshed:
            now = self.store.clock()
            due = [url for url in self.state["done"]
                   if now - self.state["checked_at"].get(url, now) >= (86400 if url in advertised else 7 * 86400)]
            if due:
                url = min(due, key=lambda value: (self.state["checked_at"].get(value, now), value))
                self.state["done"].remove(url)
                self.state["checked_at"].pop(url, None)
                self.state["pending"].insert(min(1, len(self.state["pending"])), url)
            self._refreshed = True
        self.save()

    def next(self):
        return self.state["pending"][0] if self.state["pending"] else None

    def complete(self, url):
        if url in self.state["pending"]:
            self.state["pending"].remove(url)
            self.state["done"].append(url)
            self.state["checked_at"][url] = self.store.clock()
            self.save()


class ProjectProgress:
    """A duplicate image is still considered; partial projects stay resumable."""
    def __init__(self, store, site, project_id):
        self.store = store
        self.key = "collection.project.v1:" + project_id
        with store.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (self.key,)).fetchone()
            legacy = [r[0] for r in db.execute("SELECT json_extract(document,'$.source_image_url') FROM assets WHERE json_extract(document,'$.source_id')=? AND json_extract(document,'$.project_id')=?", (site, project_id))]
        self.state = json.loads(row[0]) if row else {"complete": False, "valid": list(dict.fromkeys(x for x in legacy if x))[:3], "rejected": []}
        if not isinstance(self.state, dict) or set(self.state) != {"complete", "valid", "rejected"} or type(self.state["complete"]) is not bool or any(not isinstance(self.state[k], list) or len(self.state[k]) > 100 or any(not isinstance(x,str) for x in self.state[k]) for k in ("valid","rejected")):
            raise ValueError("invalid_project_progress")
        if len(self.state["valid"]) >= 3:
            self.state["complete"] = True

    @property
    def complete(self):
        return self.state["complete"]

    @property
    def valid_count(self):
        return len(self.state["valid"])

    def considered(self, url):
        return url in self.state["valid"] or url in self.state["rejected"]

    def save(self):
        with self.store.db() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.key, json.dumps(self.state, separators=(",", ":"))))

    def record(self, url, valid):
        key = "valid" if valid else "rejected"
        if url not in self.state[key]:
            self.state[key].append(url)
        if self.valid_count >= 3:
            self.state["complete"] = True
        self.save()

    def finish(self):
        self.state["complete"] = True
        self.save()
