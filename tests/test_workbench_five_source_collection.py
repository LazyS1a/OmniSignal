import io
import json
import time

import httpx
import pytest
from PIL import Image

from omnisignal.knowledge.design_collection import FetchError
from omnisignal.workbench.collection import WorkbenchCollector, import_browser_observation
from omnisignal.workbench.source_adapters import SOURCE_CONFIGS
from omnisignal.workbench.source_readiness import PhotoPageCursor, pexels_api_key, source_readiness
from omnisignal.workbench.store import Conflict, Store, sources


def image_bytes(number):
    output = io.BytesIO()
    Image.new("RGB", (500, 500), (number * 30, 60, 100)).save(output, "JPEG")
    return output.getvalue()


def photo(number):
    return {"id": number, "url": f"https://www.pexels.com/photo/design-{number}/",
        "width": 500, "height": 500, "alt": f"Photo {number}",
        "src": {"original": f"https://images.pexels.com/photos/{number}/photo-{number}.jpeg"}}


class Images:
    def __init__(self):
        self.calls = []
        self.failure = None

    def factory(self, hosts, reserve):
        owner = self
        class Fetcher:
            def get(self, url, *, image=False):
                assert image
                reserve()
                owner.calls.append(url)
                if owner.failure:
                    raise FetchError(owner.failure)
                return image_bytes(int(url.split("/")[-2]))
        return Fetcher()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_PEXELS_API_KEY_FILE", raising=False)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS", "pexels")
    monkeypatch.setenv("OMNISIGNAL_PEXELS_API_KEY", "fixture-key")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "1")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "0")
    clock = [1800000000]
    store = Store(tmp_path, clock=lambda: clock[0])
    plan = store.create_plan({"source_id": "pexels", "name": "Fixture photos", "query": "packaging", "request_limit": 50}, "test")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")
    return store, plan, clock


def test_pexels_real_collector_imports_and_resumes_same_cached_page(prepared):
    store, plan, _ = prepared
    requests, images = [], Images()
    def handler(request):
        requests.append(int(request.url.params["page"]))
        return httpx.Response(200, json={"photos": [photo(1), photo(2)] if requests[-1] == 1 else []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = WorkbenchCollector(store, fetcher_factory=images.factory, pexels_client=client)
        assert worker.run(plan["id"], force=True)["imported"] == 1
        cursor = PhotoPageCursor(store, plan["id"], "packaging")
        assert (cursor.state["page"], cursor.state["index"]) == (1, 1)
        # New worker models process restart, so an in-memory cursor cannot pass this test.
        worker = WorkbenchCollector(store, fetcher_factory=images.factory, pexels_client=client)
        assert worker.run(plan["id"], force=True)["imported"] == 1
        assert PhotoPageCursor(store, plan["id"], "packaging").state["page"] == 2
        assert worker.run(plan["id"], force=True)["imported"] == 0
        assert worker.run(plan["id"], force=True)["imported"] == 0
    assert requests == [1, 2]
    assert len(images.calls) == 2
    assert store.list_plans()[0]["budget"]["used"] == 4
    assets = store.list_assets(status="all")["items"]
    assert len(assets) == 2
    assert all(a["status"] == "pending" and set(a["permissions"].values()) == {"unknown"} for a in assets)
    assert all(a["collection_evidence"]["basis"] == "official-photo-api" for a in assets)
    assert all(a["width"] == 500 for a in assets)


def test_request_budget_interrupt_keeps_entire_pending_page(prepared):
    store, plan, clock = prepared
    with store.db() as db:
        db.execute("UPDATE plans SET document=json_set(document,'$.request_limit',1)")
    requests, images = [], Images()
    def handler(request):
        requests.append(request.url)
        return httpx.Response(200, json={"photos": [photo(1), photo(2)]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = WorkbenchCollector(store, fetcher_factory=images.factory, pexels_client=client)
        result = worker.run(plan["id"], force=True)
        assert result["status"] == "stopped" and result["imported"] == 0
        assert not images.calls
        cursor = PhotoPageCursor(store, plan["id"], "packaging")
        assert (cursor.state["page"], cursor.state["index"]) == (1, 0)
        clock[0] += 86400
        assert worker.run(plan["id"], force=True)["imported"] == 1
    assert len(requests) == 1
    assert len(images.calls) == 1


def test_photo_download_failure_does_not_advance_cursor(prepared):
    store, plan, _ = prepared
    images = Images()
    images.failure = "http_403"
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"photos": [photo(1)]}))) as client:
        result = WorkbenchCollector(store, fetcher_factory=images.factory, pexels_client=client).run(plan["id"], force=True)
    assert result["status"] == "failed"
    assert PhotoPageCursor(store, plan["id"], "packaging").state["index"] == 0
    assert store.list_assets(status="all")["total"] == 0
    assert store.list_plans()[0]["budget"]["used"] == 2


def test_daily_image_limit_preserves_rest_of_api_page(prepared, monkeypatch):
    store, plan, clock = prepared
    clock[0] = time.time()
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT", "100")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "1")
    images = Images()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"photos": [photo(1), photo(2)]}))) as client:
        worker = WorkbenchCollector(store, fetcher_factory=images.factory, pexels_client=client)
        result = worker.run(plan["id"], force=True)
        assert result["imported"] == 1
        assert result["acquisition"]["remaining"] == 0
        assert worker.run(plan["id"], force=True) is None
    cursor = PhotoPageCursor(store, plan["id"], "packaging")
    assert (cursor.state["page"], cursor.state["index"]) == (1, 1)
    assert len(images.calls) == 1


def test_missing_key_blocks_resume_and_existing_enabled_plan(prepared, monkeypatch):
    store, plan, _ = prepared
    monkeypatch.delenv("OMNISIGNAL_PEXELS_API_KEY")
    images = Images()
    result = WorkbenchCollector(store, fetcher_factory=images.factory).run(plan["id"], force=True)
    assert result["status"] == "failed" and "凭据" in result["reason"]
    assert not images.calls
    assert store.list_plans()[0]["budget"]["used"] == 0
    current = store.list_plans()[0]
    with pytest.raises(Conflict, match="凭据"):
        store.control_plan(plan["id"], {"action": "resume", "expected_revision": current["revision"]}, "test")


def test_private_api_key_file_preferred_without_persisting_secret(prepared, monkeypatch, tmp_path):
    store, plan, _ = prepared
    key_file = tmp_path / "private-pexels-key.txt"
    key_file.write_text("private-fixture-key\n", encoding="utf-8")
    monkeypatch.setenv("OMNISIGNAL_PEXELS_API_KEY_FILE", str(key_file.resolve()))
    assert pexels_api_key() == "private-fixture-key"
    assert source_readiness("pexels")["state"] == "ready"
    assert "private-fixture-key" not in json.dumps(store.list_plans())


@pytest.mark.parametrize("kind", ["relative", "missing", "oversize", "whitespace"])
def test_invalid_private_key_file_never_falls_back_to_environment(prepared, monkeypatch, tmp_path, kind):
    path = tmp_path / "private-invalid.txt"
    if kind == "oversize":
        path.write_bytes(b"x" * 4097)
    if kind == "whitespace":
        path.write_text("invalid embedded space")
    monkeypatch.setenv("OMNISIGNAL_PEXELS_API_KEY_FILE", "relative.txt" if kind == "relative" else str(path.resolve()))
    assert source_readiness("pexels")["state"] == "pexels_api_key_file_invalid"


def test_experimental_sources_cannot_be_enabled_implicitly(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS", raising=False)
    store = Store(tmp_path)
    for source in SOURCE_CONFIGS:
        assert not next(x for x in sources() if x["id"] == source)["verified"]
        with pytest.raises(Conflict, match="尚未通过"):
            store.create_plan({"source_id": source, "name": "No implicit activation", "query": "test"}, "test")


@pytest.mark.parametrize("source,url,image", [
    ("pinterest", "https://www.pinterest.com/pin/123/", "https://i.pinimg.com/photo.jpg"),
    ("zcool", "https://www.zcool.com.cn/work/ZABC==.html", "https://img.zcool.cn/photo.jpg"),
    ("huaban", "https://huaban.com/pins/123/", "https://hbimg.huabanimg.com/photo.webp"),
    ("behance", "https://www.behance.net/gallery/123/Design", "https://mir-s3-cdn-cf.behance.net/photo.jpg"),
])
def test_four_public_transports_reach_store_with_unknown_permissions(tmp_path, monkeypatch, source, url, image):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS", source)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_PUBLIC_HTML_SOURCE_IDS", source)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES", "0")
    store = Store(tmp_path)
    plan = store.create_plan({"source_id": source, "name": "Explicitly validated fixture"}, "test")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")
    calls = []
    def factory(hosts, reserve):
        class Fetcher:
            def get(self, requested, *, image=False):
                reserve()
                calls.append(requested)
                if image:
                    return image_bytes(1)
                if requested == SOURCE_CONFIGS[source]["listing"]:
                    return f'<main><a href="{url}">Project</a></main>'.encode()
                return f'<meta property="og:url" content="{url}"><meta property="og:title" content="Test"><meta property="og:image" content="{image_url}">'.encode()
        return Fetcher()
    image_url = image
    result = WorkbenchCollector(store, fetcher_factory=factory).run(plan["id"], force=True)
    assert result["status"] == "ok" and result["imported"] == 1
    assert calls == [SOURCE_CONFIGS[source]["listing"], url, image]
    asset = store.list_assets(status="all")["items"][0]
    assert asset["source_id"] == source
    assert asset["collection_evidence"]["basis"] == "project-detail"
    assert set(asset["permissions"].values()) == {"unknown"}


def test_browser_required_without_public_acceptance_blocks_run(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS", "pinterest")
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_PUBLIC_HTML_SOURCE_IDS", raising=False)
    store = Store(tmp_path)
    plan = store.create_plan({"source_id": "pinterest", "name": "Browser pending"}, "test")
    with pytest.raises(Conflict, match="授权浏览器"):
        store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")
    assert store.list_plans()[0]["readiness"]["state"] == "source_transport_unavailable"


def test_normal_browser_observation_import_stays_preview_and_unapproved(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS", raising=False)
    store = Store(tmp_path)
    snapshot = {"project_url": "https://www.pinterest.com/pin/123/", "title": "Observed preview",
                "images": [{"url": "https://i.pinimg.com/474x/test.jpg"}]}
    result = import_browser_observation(store, "pinterest", snapshot, image_bytes(1))
    assert result["created"]
    assert result["collection_evidence"]["basis"] == "listing-preview"
    assert set(result["permissions"].values()) == {"unknown"}
    assert not store.list_plans()
    assert not import_browser_observation(store, "pinterest", snapshot, image_bytes(1))["created"]
