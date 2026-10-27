"""Bounded pilot collection against synthetic HTML/images, with real Store budgets."""
import io

from PIL import Image
import pytest

from omnisignal.knowledge.design_collection import FetchError
from omnisignal.workbench.collection import SOURCE_CONFIGS, WorkbenchCollector
from omnisignal.workbench.store import Store


LIMIT_ENV = "OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT"


class SyntheticSource:
    def __init__(self, source="adsofbrands", count=40, invalid=0, images=1):
        self.source, self.invalid, self.images = source, invalid, images
        self.config = SOURCE_CONFIGS[source]
        self.urls = ([f"https://adsofbrands.net/en/ads/synthetic-{i}/{10000+i}" for i in range(count)]
                     if source == "adsofbrands" else
                     [f"https://packagingoftheworld.com/2026/10/synthetic-{i}.html" for i in range(count)])
        self.calls, self.articles, self.image_calls = [], [], []
        self.after_image = None
        self.blocked = None

    def image_url(self, project, number):
        if self.source == "adsofbrands":
            return f"https://adsofbrands.net/images/commercial_big/1/{100000+project*10+number}.jpeg"
        return f"https://packagingoftheworld.com/wp-content/uploads/2026/10/synthetic-{project}-{number}.jpeg"

    def article(self, index):
        pictures = "".join(f'<img src="{self.image_url(index, number)}">' for number in range(self.images))
        if self.source == "adsofbrands":
            media = ('<iframe src="https://www.youtube.com/embed/synthetic"></iframe>' if index < self.invalid
                     else f'<div class="print-image">{pictures}</div>')
            return f'''<div id="com-left"><div class="com-media">{media}</div></div>
                <div id="com-right"><h1>Synthetic outdoor {index}</h1>
                <div class="media-type"><a><span>Outdoor</span></a></div>
                <div class="description">Synthetic campaign description with enough context to exercise the supported static-media parser.</div></div>'''.encode()
        return f'''<article><h1 class="entry-title">Synthetic packaging {index}</h1>
            <div class="entry-content"><p>Synthetic packaging description with enough context to exercise the real article parser.</p>
            {"" if index < self.invalid else pictures}</div></article>'''.encode()

    def factory(self, hosts, reserve):
        source = self
        assert set(hosts) == set(self.config["hosts"])
        class Fetcher:
            def __init__(self):
                self.robots = False

            def get(self, url, *, image=False):
                if not self.robots:
                    assert reserve()
                    source.calls.append("robots")
                    self.robots = True
                    if source.blocked == "robots_denied":
                        raise FetchError("robots_denied")
                assert reserve()
                source.calls.append(url)
                if url == source.config["listing"]:
                    return "".join(f'<a class="item" href="{item}">case</a>' for item in source.urls).encode()
                if image:
                    source.image_calls.append(url)
                    picture = io.BytesIO()
                    colour = (len(source.image_calls) * 7 % 255, 80, 120)
                    Image.new("RGB", (500, 600), colour).save(picture, "JPEG")
                    if source.after_image:
                        source.after_image()
                    return picture.getvalue()
                source.articles.append(url)
                if source.blocked:
                    raise FetchError(source.blocked)
                return source.article(source.urls.index(url))
        return Fetcher()


@pytest.fixture(autouse=True)
def clear_limit(monkeypatch):
    monkeypatch.delenv(LIMIT_ENV, raising=False)


def prepared(tmp_path, network, budget=100, enabled=True):
    store = Store(tmp_path)
    plan = store.create_plan({"name": "Synthetic pilot", "source_id": network.source, "request_limit": budget}, "test")
    if enabled:
        plan = store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")
    return store, plan, WorkbenchCollector(store, fetcher_factory=network.factory)


def test_first_three_video_projects_do_not_starve_supported_static_projects(tmp_path):
    network = SyntheticSource(invalid=3)
    store, plan, worker = prepared(tmp_path, network, budget=12)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "partial" and result["imported"] == 2
    assert network.articles == network.urls[:5]
    assert len(network.image_calls) == 2 and len(network.calls) == 9
    assert store.list_plans()[0]["budget"]["used"] == 9
    assets = store.list_assets(status="all")["items"]
    assert {item["source_url"] for item in assets} == set(network.urls[3:5])
    assert all(item["status"] == "pending" and item["kind"] == "uncertain" and not item["purposes"] for item in assets)
    assert all(set(item["permissions"].values()) == {"unknown"} for item in assets)
    assert not any("youtube" in url for url in network.calls)


@pytest.mark.parametrize("source", ["adsofbrands", pytest.param("potw", marks=pytest.mark.skipif(
    "potw" not in SOURCE_CONFIGS, reason="Optional POTW adapter is not installed"))])
def test_default_retains_two_successful_projects_and_three_images_each(tmp_path, source):
    network = SyntheticSource(source=source, images=4)
    store, plan, worker = prepared(tmp_path, network)
    assert worker.run(plan["id"], force=True)["imported"] == 6
    assert network.articles == network.urls[:2] and len(network.image_calls) == 6
    assert len({item["project_id"] for item in store.list_assets(status="all")["items"]}) == 2


@pytest.mark.parametrize("configured,expected", [("1", 1), ("2", 2), ("5", 5), ("10", 10)])
def test_explicit_limits_bound_successful_projects(tmp_path, monkeypatch, configured, expected):
    monkeypatch.setenv(LIMIT_ENV, configured)
    network = SyntheticSource(images=3)
    store, plan, worker = prepared(tmp_path, network, budget=60)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "ok" and result["imported"] == expected * 3
    assert len(network.articles) == expected and len(network.calls) == 2 + expected * 4
    assert store.list_plans()[0]["budget"]["request_limit"] == 60
    assert all(set(item["permissions"].values()) == {"unknown"} for item in store.list_assets(status="all", page_size=60)["items"])


@pytest.mark.parametrize("configured", ["", "0", "-1", "11", "9999", "unlimited", "2.0", "+2", "02", " 10 ", "True", "１０"])
def test_invalid_configuration_strictly_falls_back_to_two(tmp_path, monkeypatch, configured):
    monkeypatch.setenv(LIMIT_ENV, configured)
    network = SyntheticSource()
    _, plan, worker = prepared(tmp_path, network)
    assert worker.run(plan["id"], force=True)["imported"] == 2
    assert network.articles == network.urls[:2]


@pytest.mark.parametrize("configured,attempts", [(None, 6), ("1", 6), ("10", 30), ("bad", 6)])
def test_all_invalid_candidates_stop_at_separate_attempt_bound(tmp_path, monkeypatch, configured, attempts):
    if configured is not None:
        monkeypatch.setenv(LIMIT_ENV, configured)
    network = SyntheticSource(invalid=40)
    store, plan, worker = prepared(tmp_path, network)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "partial" and result["imported"] == 0
    assert len(network.articles) == attempts and len(network.calls) == attempts + 2
    assert not network.image_calls and store.list_assets(status="all")["total"] == 0


def test_ten_project_limit_cannot_exceed_existing_request_budget(tmp_path, monkeypatch):
    monkeypatch.setenv(LIMIT_ENV, "10")
    network = SyntheticSource(images=3)
    store, plan, worker = prepared(tmp_path, network, budget=6)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "stopped" and result["imported"] == 3
    assert len(network.calls) == 6 and network.articles == network.urls[:1]
    assert store.list_plans()[0]["budget"]["used"] == 6
    assert worker.run(plan["id"], force=True) is None


def test_pause_with_large_project_limit_still_blocks_next_request(tmp_path, monkeypatch):
    monkeypatch.setenv(LIMIT_ENV, "10")
    network = SyntheticSource(images=3)
    store, plan, worker = prepared(tmp_path, network)
    network.after_image = lambda: store.control_plan(plan["id"], {"action": "pause", "expected_revision": plan["revision"]}, "test")
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "stopped" and result["imported"] == 1
    assert len(network.calls) == 4
    assert not store.list_plans(raw=True)[0]["enabled"]


def test_paused_plan_never_fetches_even_with_explicit_large_limit(tmp_path, monkeypatch):
    monkeypatch.setenv(LIMIT_ENV, "10")
    network = SyntheticSource()
    _, plan, worker = prepared(tmp_path, network, enabled=False)
    assert worker.run(plan["id"], force=True) is None and not network.calls


@pytest.mark.parametrize("code", ["robots_denied", "http_403", "http_429", "access_barrier"])
def test_access_restriction_never_becomes_skip_and_continue(tmp_path, monkeypatch, code):
    monkeypatch.setenv(LIMIT_ENV, "10")
    network = SyntheticSource()
    network.blocked = code
    store, plan, worker = prepared(tmp_path, network)
    result = worker.run(plan["id"], force=True)
    assert result["status"] == "failed" and result["imported"] == 0
    assert len(network.articles) <= 1 and not network.image_calls
    assert store.list_assets(status="all")["total"] == 0
