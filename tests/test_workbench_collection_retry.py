"""Offline regression: failed front-page cases must not starve later cases."""
import json

import pytest

from omnisignal.workbench.acquisition import ProjectProgress, ProjectRetry
from omnisignal.workbench.collection import WorkbenchCollector, project_id
from omnisignal.workbench.store import Store
from test_workbench_collection_limits import SyntheticSource, prepared


class ArchiveSource(SyntheticSource):
    def __init__(self):
        super().__init__(count=46, invalid=36)
        self.archives = [f"https://adsofbrands.net/en/ads?media=4&page={i}" for i in range(2, 8)]
        self.bad_archive = False

    def article(self, index):
        if self.bad_archive and index == 40:
            return b"<html><body>Changed article layout</body></html>"
        return super().article(index)

    def factory(self, hosts, reserve):
        base = super().factory(hosts, reserve)
        source = self

        class Fetcher:
            def get(self, url, *, image=False):
                if url in source.archives:
                    assert reserve()
                    source.calls.append(url)
                    index = 40 + source.archives.index(url)
                    return f'<a class="item" href="{source.urls[index]}">case</a>'.encode()
                value = base.get(url, image=image)
                if url == source.config["listing"]:
                    value += ''.join(f'<a href="{page}">next</a>' for page in source.archives).encode()
                return value

        return Fetcher()


def daily(monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "100")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "12")


def cursor(store):
    with store.db() as db:
        return json.loads(db.execute("SELECT value FROM meta WHERE key='collection.archive.v1:adsofbrands'").fetchone()[0])


def test_failed_front_page_cools_down_and_later_static_cases_and_archives_progress(tmp_path, monkeypatch):
    daily(monkeypatch)
    source = ArchiveSource()
    store, plan, worker = prepared(tmp_path, source, budget=200)
    first = worker.run(plan["id"], force=True)
    assert first["status"] == "partial" and first["imported"] == 0
    assert len(source.calls) == 38 and source.articles == source.urls[:36]
    assert len(cursor(store)["pending"]) == 6 and not cursor(store)["done"]

    # A normal later run with the same request allowance must reach four
    # supported root cases and three advertised archives, without retrying
    # the failed prefix or declaring it permanently complete.
    # A worker restart must use the persisted failure/cursor receipts.
    store = Store(tmp_path)
    worker = WorkbenchCollector(store, fetcher_factory=source.factory)
    second = worker.run(plan["id"], force=True)
    assert second["imported"] == 7
    assert source.articles[36:] == source.urls[36:43]
    assert len(cursor(store)["done"]) == 3 and len(cursor(store)["pending"]) == 3
    assert store.list_plans()[0]["budget"]["used"] == len(source.calls) == 57
    assert not any("youtube" in url for url in source.calls)
    assets = store.list_assets(status="all")["items"]
    assert all(set(item["permissions"].values()) == {"unknown"} for item in assets)

    # Failed cases remain eligible for a bounded future check if the source
    # becomes supported. No old request budget is manually reset.
    now = store.clock()
    store.clock = lambda: now + 86401
    source.invalid = 0
    previous = len(source.articles)
    third = worker.run(plan["id"], force=True)
    assert third["imported"] == 12
    assert source.articles[previous:] == source.urls[:12]


def test_failed_archive_stays_pending_and_does_not_block_other_pages(tmp_path, monkeypatch):
    daily(monkeypatch)
    source = ArchiveSource()
    source.bad_archive = True
    store, plan, worker = prepared(tmp_path, source, budget=200)
    worker.run(plan["id"], force=True)
    second = worker.run(plan["id"], force=True)
    assert second["imported"] == 6
    saved = cursor(store)
    assert source.archives[0] in saved["pending"]
    assert source.archives[0] not in saved["done"]
    assert saved["done"] == source.archives[1:3]
    assert saved["pending"] == source.archives[3:] + source.archives[:1]


def test_all_invalid_images_remain_pending_and_are_revisited_only_when_due(tmp_path, monkeypatch):
    daily(monkeypatch)
    source = SyntheticSource(count=1)
    original_factory = source.factory
    invalid = [True]

    def factory(hosts, reserve):
        base = original_factory(hosts, reserve)

        class Fetcher:
            def get(self, url, *, image=False):
                value = base.get(url, image=image)
                return b"not an image" if image and invalid[0] else value

        return Fetcher()

    source.factory = factory
    store, plan, worker = prepared(tmp_path, source, budget=200)
    identity = project_id("adsofbrands", source.urls[0])
    assert worker.run(plan["id"], force=True)["imported"] == 0
    assert not ProjectProgress(store, "adsofbrands", identity).complete
    assert ProjectRetry(store, identity).state["reason_code"] == "no_usable_images"
    calls = len(source.calls)
    assert worker.run(plan["id"], force=True)["imported"] == 0
    assert len(source.calls) == calls + 2  # Only robots and listing; no image retry.
    now = store.clock()
    store.clock = lambda: now + 86401
    invalid[0] = False
    assert worker.run(plan["id"], force=True)["imported"] == 1
    assert ProjectProgress(store, "adsofbrands", identity).complete
    assert ProjectRetry(store, identity).state is None


@pytest.mark.parametrize("code", ["robots_denied", "http_403", "http_429", "access_barrier"])
def test_access_refusals_stop_without_becoming_deferred_parse_failures(tmp_path, monkeypatch, code):
    daily(monkeypatch)
    source = SyntheticSource(count=1)
    source.blocked = code
    store, plan, worker = prepared(tmp_path, source, budget=200)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "failed" and result["imported"] == 0
    assert len(source.articles) <= 1
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'collection.project-retry.v1:%'").fetchone()[0] == 0


def test_budget_interruption_preserves_remaining_project_without_backoff(tmp_path, monkeypatch):
    daily(monkeypatch)
    source = SyntheticSource(count=1, images=3)
    store, plan, worker = prepared(tmp_path, source, budget=4)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "stopped" and result["imported"] == 1
    assert len(source.calls) == store.list_plans()[0]["budget"]["used"] == 4
    identity = project_id("adsofbrands", source.urls[0])
    assert not ProjectProgress(store, "adsofbrands", identity).complete
    assert ProjectRetry(store, identity).state is None


def test_manual_pause_does_not_turn_an_interrupted_project_into_parse_backoff(tmp_path, monkeypatch):
    daily(monkeypatch)
    source = SyntheticSource(count=1, images=3)
    store, plan, worker = prepared(tmp_path, source, budget=200)
    source.after_image = lambda: store.control_plan(plan["id"], {
        "action": "pause", "expected_revision": plan["revision"]}, "test")
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "stopped" and result["imported"] == 1
    assert len(source.calls) == 4
    identity = project_id("adsofbrands", source.urls[0])
    assert not ProjectProgress(store, "adsofbrands", identity).complete
    assert ProjectRetry(store, identity).state is None


def test_unexpected_parser_error_text_is_not_persisted(tmp_path, monkeypatch):
    daily(monkeypatch)
    source = SyntheticSource(count=1)
    store, plan, worker = prepared(tmp_path, source, budget=200)

    def unexpected_parser(*args):
        raise ValueError("synthetic private response text must not be retained")

    monkeypatch.setattr("omnisignal.workbench.collection._extract", unexpected_parser)
    assert worker.run(plan["id"], force=True)["status"] == "partial"
    state = ProjectRetry(store, project_id("adsofbrands", source.urls[0])).state
    assert state["reason_code"] == "article_parse"
    assert "private" not in json.dumps(state)


def test_retry_backoff_is_persisted_bounded_and_never_permanent(tmp_path):
    now = [1000000.0]
    store = Store(tmp_path, clock=lambda: now[0])
    for days in [1, 2, 4, 7, 7]:
        receipt = ProjectRetry(store, "synthetic-project")
        assert not receipt.deferred
        receipt.fail("article_parse")
        receipt = ProjectRetry(store, "synthetic-project")
        assert receipt.deferred
        assert receipt.state["next_attempt_at"] - now[0] == days * 86400
        now[0] = receipt.state["next_attempt_at"]
        assert not ProjectRetry(store, "synthetic-project").deferred
