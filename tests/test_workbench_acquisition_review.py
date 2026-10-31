"""Independent acquisition regression cases; synthetic HTML and local images only."""
import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from omnisignal.workbench.acquisition import ArchiveCursor, listing_links, progress
from omnisignal.workbench.collection import WorkbenchCollector, project_id
from omnisignal.workbench.store import Store


def picture(number):
    output = io.BytesIO()
    Image.new("RGB", (500, 500), (number % 256, number // 256, 96)).save(output, "PNG")
    return output.getvalue()


class Pages:
    root = "https://bpando.org/"
    archive = "https://bpando.org/page/2/"

    def __init__(self, root_projects, archive_projects=(), images=None):
        self.root_projects, self.archive_projects = root_projects, archive_projects
        self.images = images or {}
        self.calls = []

    @staticmethod
    def article_url(number):
        return f"https://bpando.org/2026/10/30/review-{number}/"

    def factory(self, hosts, reserve):
        source = self

        class Fetcher:
            def get(self, url, *, image=False):
                assert reserve()
                source.calls.append(url)
                if image:
                    return picture(int(url.rsplit("/", 1)[-1].split(".")[0]))
                if url in {source.root, source.archive}:
                    if url == source.archive and getattr(source, "empty_archive", False):
                        return b"<html><p>Temporary listing placeholder</p></html>"
                    projects = source.root_projects if url == source.root else source.archive_projects
                    links = "".join(f'<a href="{source.article_url(number)}">project</a>' for number in projects)
                    if url == source.root and source.archive_projects:
                        links += '<a href="/page/2/">Next</a>'
                    return links.encode()
                number = int(url.rstrip("/").rsplit("-", 1)[-1])
                numbers = source.images.get(number, [number * 3 + offset for offset in range(3)])
                images = "".join(f'<img src="https://bpando.org/{value}.png" width="500" height="500">' for value in numbers)
                text = "Synthetic design project describing typography, layout, colour, materials and printed packaging. " * 3
                return f'<div class="article-content"><h1>Project {number}</h1><div class="wysiwyg--post"><p>{text}</p>{images}</div></div>'.encode()

        return Fetcher()


def prepared(tmp_path, monkeypatch, source, *, budget=500, target=100, project_limit=100):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", str(target))
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", str(project_limit))
    clock = [datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr("omnisignal.workbench.store.stamp", lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat())
    store = Store(tmp_path, clock=lambda: clock[0])
    plan = store.create_plan({"name": "Acquisition review", "source_id": "bpando", "request_limit": budget}, "test")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")
    worker = WorkbenchCollector(store, fetcher_factory=source.factory, clock=lambda: clock[0])
    return store, plan["id"], worker, clock


def test_daily_cap_partial_project_resumes_its_remaining_images_next_day(tmp_path, monkeypatch):
    source = Pages([1])
    store, plan, worker, clock = prepared(tmp_path, monkeypatch, source, target=2)
    assert worker.run(plan)["imported"] == 2
    assert progress(store)["remaining"] == 0
    clock[0] += 86400
    result = worker.run(plan, force=True)
    assert result["imported"] == 1, "A daily cap must not mark a partially acquired project complete"
    assert store.list_assets(status="all")["total"] == 3


def test_budget_interruption_keeps_archive_project_remaining_images_retryable(tmp_path, monkeypatch):
    source = Pages([1], [2])
    store, plan, worker, clock = prepared(tmp_path, monkeypatch, source, budget=9)
    # root(1) + project 1(4) + archive(1) + project 2 HTML(1) + 2 images(2)
    first = worker.run(plan)
    assert first["status"] == "stopped" and first["imported"] == 5
    assert len(source.calls) == 9
    assert ArchiveCursor(store, "bpando", source.root).next() == source.archive
    clock[0] += 86400
    second = worker.run(plan, force=True)
    assert second["imported"] == 1, "Retaining a page URL is insufficient if its partially acquired project is skipped"
    assert store.list_assets(status="all")["total"] == 6


def test_duplicate_only_root_project_does_not_starve_archive_forever(tmp_path, monkeypatch):
    source = Pages([1], [2], images={1: [71]})
    store, plan, worker, _ = prepared(tmp_path, monkeypatch, source, project_limit=1)
    store.import_asset({"title": "Existing manual reference", "source_url": "https://bpando.org/old", "project_id": "manual-original"}, picture(71))
    assert worker.run(plan)["imported"] == 0
    worker.run(plan, force=True)
    assert source.archive in source.calls, "A fully duplicate project must be durably considered, not consume every subsequent run"
    assert progress(store)["new_unique_images"] > 0


def test_cross_project_dedup_preserves_the_human_record_and_permissions(tmp_path, monkeypatch):
    source = Pages([1], images={1: [71]})
    store, plan, worker, _ = prepared(tmp_path, monkeypatch, source)
    original = store.import_asset({"title": "Human title", "source_url": "https://bpando.org/original", "project_id": "manual-original"}, picture(71))
    store.review(original["id"], {"expected_revision": 1, "status": "excluded", "reason": "Do not use", "permissions": {"display": "denied", "analysis": "denied", "export": "denied"}, "permission_note": "Synthetic owner restriction"}, "test", "admin")
    before = store.asset(original["id"])
    assert worker.run(plan)["imported"] == 0
    after = store.asset(original["id"])
    assert after == before
    assert progress(store)["new_unique_images"] == 0


def test_advertised_invalid_port_does_not_discard_other_safe_pagination():
    page = '<a href="https://bpando.org:invalid/page/2/">bad</a><a href="/page/3/">next</a>'
    assert listing_links("bpando", page, "https://bpando.org/") == ["https://bpando.org/page/3/"]


def test_empty_archive_discovery_keeps_page_pending_instead_of_declaring_it_done(tmp_path, monkeypatch):
    source = Pages([1], [2])
    source.empty_archive = True
    store, plan, worker, _ = prepared(tmp_path, monkeypatch, source)
    result = worker.run(plan)
    assert result["imported"] == 3
    assert ArchiveCursor(store, "bpando", source.root).next() == source.archive, "An unparseable archive page must not become a completed page"
    assert result["status"] == "partial"
    source.empty_archive = False
    assert worker.run(plan, force=True)["imported"] == 3


def test_completed_page_is_rechecked_after_numeric_page_contents_shift(tmp_path, monkeypatch):
    source = Pages([1], [2])
    store, plan, worker, clock = prepared(tmp_path, monkeypatch, source)
    assert worker.run(plan)["imported"] == 6
    source.archive_projects = [2, 3]  # same URL, new previously unseen project
    assert worker.run(plan, force=True)["imported"] == 0
    clock[0] += 86400
    result = worker.run(plan, force=True)
    assert result["imported"] == 3
    assert progress(store)["new_unique_images"] == 3
    assert store.list_assets(status="all")["total"] == 9


def test_refresh_is_bounded_to_one_and_preserves_incomplete_first_page(tmp_path, monkeypatch):
    source = Pages([1], [2])
    store, _, _, clock = prepared(tmp_path, monkeypatch, source)
    cursor = ArchiveCursor(store, "bpando", source.root)
    page = b'<a href="/page/2/">2</a><a href="/page/3/">3</a><a href="/page/4/">4</a>'
    cursor.observe(page, source.root)
    cursor.complete('https://bpando.org/page/2/')
    cursor.complete('https://bpando.org/page/3/')
    clock[0] += 86400
    reloaded = ArchiveCursor(store, "bpando", source.root)
    reloaded.observe(page, source.root)
    assert reloaded.next() == 'https://bpando.org/page/4/'
    assert len(reloaded.state['pending']) == 2
    assert len(reloaded.state['done']) == 1
    reloaded.observe(page, source.root)  # do not spend an extra refresh slot
    assert len(reloaded.state['pending']) == 2


def test_unadvertised_completed_pages_become_due_weekly_and_old_cursor_migrates(tmp_path, monkeypatch):
    source = Pages([1], [2])
    store, _, _, clock = prepared(tmp_path, monkeypatch, source)
    with store.db() as db:
        db.execute("INSERT INTO meta(key,value) VALUES(?,?)", ('collection.archive.v1:bpando', json.dumps({'pending': [], 'done': [source.archive]})))
    cursor = ArchiveCursor(store, "bpando", source.root)
    cursor.observe(b'<a href="/">Home</a>', source.root)
    assert cursor.next() is None
    clock[0] += 7 * 86400
    cursor = ArchiveCursor(store, "bpando", source.root)
    cursor.observe(b'<a href="/">Home</a>', source.root)
    assert cursor.next() == source.archive


def test_bad_ipv6_link_does_not_abort_valid_pagination():
    page = '<a href="https://[invalid/page/2/">bad</a><a href="/page/3/">next</a>'
    assert listing_links("bpando", page, "https://bpando.org/") == ['https://bpando.org/page/3/']
