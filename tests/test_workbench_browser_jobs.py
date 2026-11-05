"""End-to-end scheduling/storage tests with an offline browser transport."""
import io
import json
import time
from datetime import datetime, timezone

import pytest
from PIL import Image

from omnisignal.knowledge.design_collection import FetchError, PublicFetcher
from omnisignal.workbench.browser_jobs import BrowserJobCursor, session_state
from omnisignal.workbench.collection import WorkbenchCollector
from omnisignal.workbench.store import Store


def jpeg(number):
    output = io.BytesIO()
    Image.new("RGB", (600, 700), (number * 20, 70, 110)).save(output, "JPEG")
    return output.getvalue()


class OfflineTransport:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.deny = False
        self.image_identity = {1: 1, 2: 2, 3: 3}

    def fetcher(self, hosts, reserve):
        parent = self
        class Fetcher:
            def check_access(self, url):
                if parent.deny:
                    raise FetchError("robots_denied")
                return True

            def get(self, url, *, image=False):
                assert image
                reserve()
                parent.calls.append(("image", url))
                return jpeg(parent.image_identity[int(url.rsplit("/", 1)[1].split(".")[0])])
        return Fetcher()

    def browser(self, config, reserve, access_policy, *, image_fetcher):
        parent = self
        class Browser:
            def open(self):
                access_policy(config.listing_url)

            def discover(self, *, limit, resume_cursor=None):
                reserve()
                parent.calls.append(("listing", config.listing_url))
                return {"state": "ready", "project_urls": [f"https://www.pinterest.com/pin/{i}/" for i in (1, 2, 3)],
                    "resume_cursor": None, "pagination_state": "pagination_unverified"}

            def detail(self, url):
                from omnisignal.workbench.browser_collection import BrowserCollectionError
                reserve()
                parent.calls.append(("detail", url))
                if parent.failure:
                    raise BrowserCollectionError(parent.failure)
                number = int(url.rstrip("/").rsplit("/", 1)[1])
                return {"snapshot": {"project_url": url, "title": f"Poster {number}",
                    "description": "Observed public project", "images": [{"url": f"https://i.pinimg.com/originals/{number}.jpg", "width": 600, "height": 700}]}}

            def get_image(self, url):
                return image_fetcher(url)

            def close(self):
                pass
        return Browser()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    config = tmp_path / "browser.json"
    config.write_text(json.dumps({"version": 1, "sources": {"pinterest": {
        "profile_dir": str(tmp_path / "private-profile"), "listing_url": "https://www.pinterest.com/"}}}), encoding="utf-8")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_BROWSER_CONFIG", str(config))
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS", "pinterest")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "500")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_SOURCE_QUOTAS", '{"pinterest":2,"adsofbrands":498}')
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "100")
    now = [time.time()]
    monkeypatch.setattr("omnisignal.workbench.store.stamp", lambda: datetime.fromtimestamp(now[0], timezone.utc).isoformat())
    store = Store(tmp_path / "store", clock=lambda: now[0])
    plan = store.create_plan({"name": "Offline browser", "source_id": "pinterest", "interval_hours": 1, "request_limit": 50}, "test")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")
    transport = OfflineTransport()
    return store, plan, now, transport


def worker(store, transport):
    return WorkbenchCollector(store, fetcher_factory=transport.fetcher, browser_factory=transport.browser, clock=store.clock)


def test_due_job_imports_into_real_store_and_quota_preserves_tail(prepared):
    store, plan, now, transport = prepared
    result = worker(store, transport).sweep()
    assert result[0]["imported"] == 2
    assets = store.list_assets(status="all")["items"]
    assert len(assets) == 2
    assert all(a["status"] == "pending" and set(a["permissions"].values()) == {"unknown"} for a in assets)
    assert all(a["width"] == 600 and a["height"] == 700 and a["source_id"] == "pinterest" for a in assets)
    assert all(a["collection_evidence"]["basis"] == "project-detail" for a in assets)
    assert BrowserJobCursor(store, plan["id"], "pinterest", "https://www.pinterest.com/").state["pending"] == ["https://www.pinterest.com/pin/3/"]
    assert worker(store, transport).run(plan["id"], force=True) is None
    now[0] += 86400
    # A fresh worker consumes the saved pending item, not the same feed again.
    assert worker(store, transport).sweep()[0]["imported"] == 1
    assert len([c for c in transport.calls if c[0] == "listing"]) == 1
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE actor='collector' AND action='import'").fetchone()[0] == 3


def test_hash_duplicate_does_not_create_a_second_asset(prepared, monkeypatch):
    store, plan, _, transport = prepared
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_SOURCE_QUOTAS", '{"pinterest":500}')
    transport.image_identity[3] = 1
    assert worker(store, transport).sweep()[0]["imported"] == 2
    assert worker(store, transport).run(plan["id"], force=True)["imported"] == 0
    assert store.list_assets(status="all")["total"] == 2


def test_login_failure_is_durable_and_normal_polling_does_not_retry(prepared):
    store, plan, now, transport = prepared
    transport.failure = "login_required"
    result = worker(store, transport).sweep()[0]
    assert result["status"] == "failed" and result["imported"] == 0
    assert session_state(store, "pinterest")["state"] == "expired"
    count = len(transport.calls)
    now[0] += 86400
    assert worker(store, transport).sweep() == []
    assert len(transport.calls) == count
    transport.failure = None
    assert worker(store, transport).run(plan["id"], force=True)["imported"] == 2


def test_robots_denial_is_not_retried_through_browser(prepared):
    store, plan, _, transport = prepared
    transport.deny = True
    result = worker(store, transport).sweep()[0]
    assert result["status"] == "failed"
    assert not transport.calls
    assert session_state(store, "pinterest")["reason_code"] == "robots_denied"


def test_pause_and_resume_keeps_pending_projects(prepared):
    store, plan, now, transport = prepared
    worker(store, transport).sweep()
    current = store.list_plans()[0]
    store.control_plan(plan["id"], {"action": "pause", "expected_revision": current["revision"]}, "test")
    now[0] += 86400
    before = len(transport.calls)
    assert worker(store, transport).sweep() == []
    assert len(transport.calls) == before
    current = store.list_plans()[0]
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": current["revision"]}, "test")
    assert worker(store, transport).sweep()[0]["imported"] == 1


def test_browser_access_policy_and_http_share_robots_cache():
    fetcher = PublicFetcher(["www.pinterest.com"])
    calls = []
    def request(url, cap):
        calls.append(url)
        return b"User-agent: *\nDisallow: /pin/\n", "text/plain"
    fetcher._request = request
    assert fetcher.check_access("https://www.pinterest.com/")
    with pytest.raises(FetchError, match="robots_denied"):
        fetcher.get("https://www.pinterest.com/pin/1/")
    assert calls == ["https://www.pinterest.com/robots.txt"]
