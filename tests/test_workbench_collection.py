import io
import socket
import threading
import urllib.error

import pytest
from PIL import Image

from omnisignal.knowledge.design_collection import FetchError
from omnisignal.knowledge.design_sources import SOURCES
from omnisignal.workbench.collection import BudgetedFetcher, WorkbenchCollector, project_id
from omnisignal.workbench.store import Store


def image_bytes(number):
    output = io.BytesIO()
    Image.new("RGB", (500, 500), (number * 30, 60, 100)).save(output, "JPEG")
    return output.getvalue()


def document(url):
    title = url.rstrip("/").rsplit("/", 1)[-1]
    text = "This synthetic branding reference describes lettering, layout, illustration and printed packaging. " * 3
    images = "".join(f'<img src="https://bpando.org/{title}-{n}.jpg" width="500" height="500">' for n in range(1, 5))
    return f'<div class="article-content"><h1>{title}</h1><div class="wysiwyg--post"><p>{text}</p>{images}</div></div>'.encode()


class FakeNetwork:
    def __init__(self):
        self.urls = [f"https://bpando.org/2026/10/23/project-{i}/" for i in range(1, 4)]
        self.calls = []
        self.after_image = None
        self.failure = None

    def factory(self, hosts, reserve):
        network = self

        class Fetcher:
            def __init__(self):
                self.robots = False

            def get(self, url, *, image=False):
                if not self.robots:
                    reserve()
                    network.calls.append("https://bpando.org/robots.txt")
                    self.robots = True
                reserve()
                network.calls.append(url)
                if network.failure:
                    raise FetchError(network.failure)
                if image:
                    data = image_bytes(int(url[-5]))
                    if network.after_image:
                        network.after_image()
                    return data
                if url == SOURCES["bpando"]["listing"]:
                    return "".join(f'<a href="{item}">case</a>' for item in network.urls).encode()
                return document(url)

        return Fetcher()


@pytest.fixture
def setup(tmp_path):
    clock = [1_700_000_000]
    store = Store(tmp_path, clock=lambda: clock[0])
    plan = store.create_plan({"name": "Reference trial", "source_id": "bpando", "request_limit": 50}, "test")
    network = FakeNetwork()
    worker = WorkbenchCollector(store, fetcher_factory=network.factory, clock=lambda: clock[0])
    return store, plan, network, worker, clock


def enable(store, plan):
    return store.control_plan(plan["id"], {"action": "resume", "expected_revision": plan["revision"]}, "test")


def test_creating_paused_plan_does_not_fetch(setup):
    store, plan, network, worker, _ = setup
    assert worker.sweep() == []
    assert worker.run(plan["id"], force=True) is None
    assert not network.calls
    assert store.worker_status()["status"] == "healthy"


def test_two_projects_three_images_each_all_pending_no_demo_seed(setup):
    store, plan, network, worker, _ = setup
    enable(store, plan)
    result = worker.sweep()[0]
    assert result["status"] == "ok" and result["imported"] == 6
    assert len(network.calls) == 10  # robots + listing + 2 * (article + 3 images)
    assert SOURCES["bpando"]["seed"] not in network.calls
    assert network.urls[2] not in network.calls
    assets = store.list_assets(status="all")["items"]
    assert len({a["project_id"] for a in assets}) == 2
    assert all(a["status"] == "pending" and a["purposes"] == [] for a in assets)
    assert all(set(a["permissions"].values()) == {"unknown"} for a in assets)
    assert store.list_plans()[0]["last_run"]["requests"] == 10


def test_known_projects_skip_network_and_do_not_block_new_articles(setup):
    store, plan, network, worker, clock = setup
    enable(store, plan)
    worker.sweep()
    original = store.list_assets(status="all")["items"][0]
    store.review(original["id"], {"expected_revision": 1, "title": "Human title", "reason": "Human judgment"}, "editor", "operator")
    network.calls.clear()
    assert worker.sweep() == []
    clock[0] += 24 * 3600 + 1
    result = worker.sweep()[0]
    assert result["imported"] == 3
    assert network.urls[0] not in network.calls and network.urls[1] not in network.calls
    assert network.urls[2] in network.calls
    assert store.asset(original["id"])["title"] == "Human title"
    assert store.list_assets(status="all")["total"] == 9


def test_budget_counts_robots_and_stops_before_article(setup):
    store, plan, network, worker, _ = setup
    with store.db() as db:
        db.execute("UPDATE plans SET document=json_set(document,'$.request_limit',2)")
    enable(store, plan)
    result = worker.sweep()[0]
    assert result["status"] == "stopped" and "预算" in result["reason"]
    assert network.calls == ["https://bpando.org/robots.txt", SOURCES["bpando"]["listing"]]
    assert store.list_plans()[0]["budget"]["used"] == 2
    assert worker.run(plan["id"], force=True) is None


def test_pause_finishes_inflight_image_but_blocks_next_request(setup):
    store, plan, network, worker, _ = setup
    enabled = enable(store, plan)
    network.after_image = lambda: store.control_plan(plan["id"], {"action": "pause", "expected_revision": enabled["revision"]}, "test")
    result = worker.sweep()[0]
    assert result["status"] == "stopped" and "暂停" in result["reason"]
    assert result["imported"] == 1
    assert len(network.calls) == 4
    assert store.list_plans()[0]["budget"]["used"] == 4


def test_failed_http_attempt_keeps_budget_and_has_safe_reason(setup):
    store, plan, network, worker, _ = setup
    enable(store, plan)
    network.failure = "http_403"
    result = worker.sweep()[0]
    assert result["status"] == "failed" and "拒绝" in result["reason"]
    assert store.list_plans()[0]["budget"]["used"] == 2
    assert not store.list_plans(raw=True)[0]["active_run"]


def test_invalid_images_are_not_imported(setup, monkeypatch):
    store, plan, network, worker, _ = setup
    enable(store, plan)
    monkeypatch.setattr("omnisignal.workbench.collection.inspect_image", lambda *_: (_ for _ in ()).throw(ValueError("invalid")))
    result = worker.sweep()[0]
    assert result["status"] == "partial" and result["imported"] == 0
    assert store.list_assets(status="all")["total"] == 0


def test_project_identity_ignores_tracking_and_trailing_slash():
    assert project_id("bpando", "https://bpando.org/2026/10/23/project/?utm=x") == project_id("bpando", "https://bpando.org/2026/10/23/project")


def test_budgeted_fetcher_counts_actual_robots_and_failed_content(monkeypatch):
    calls, reserved = [], []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, *_): return b""
        @property
        def headers(self): return self
        def get_content_type(self): return "text/plain"

    class Opener:
        def open(self, request, **_):
            calls.append(request.full_url)
            if not request.full_url.endswith("robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 403, "Denied", {}, None)
            return Response()

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("1.1.1.1", 443))])
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    fetcher = BudgetedFetcher(["bpando.org"], lambda: reserved.append(True) or True)
    monkeypatch.setattr(fetcher.stop_event, "wait", lambda _: False)
    with pytest.raises(FetchError, match="http_403"):
        fetcher.get(SOURCES["bpando"]["listing"])
    assert len(calls) == len(reserved) == 2


def test_pause_during_host_wait_is_checked_before_next_http(monkeypatch):
    paused, calls = [], []
    class Opener:
        def open(self, request, **_): calls.append(request.full_url)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("1.1.1.1", 443))])
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    fetcher = BudgetedFetcher(["bpando.org"], lambda: not paused)
    monkeypatch.setattr(fetcher.stop_event, "wait", lambda _: paused.append(True) or False)
    import time
    fetcher.last["bpando.org"] = time.monotonic()
    with pytest.raises(FetchError, match="request_blocked"):
        fetcher._request("https://bpando.org/", 100)
    assert paused and not calls


def test_invalid_network_scope_never_consumes_request_budget(monkeypatch):
    reservations = []
    fetcher = BudgetedFetcher(["bpando.org"], lambda: reservations.append(True) or True)
    with pytest.raises(FetchError, match="outside_host_scope"):
        fetcher.get("https://elsewhere.invalid/")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(FetchError, match="non_public_address"):
        fetcher.get("https://bpando.org/")
    assert reservations == []


def test_stop_interrupts_rate_wait_before_any_http_request(monkeypatch):
    entered, reserved = threading.Event(), []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("1.1.1.1", 443))])
    fetcher = BudgetedFetcher(["bpando.org"], lambda: reserved.append(True) or True)
    import time
    fetcher.last["bpando.org"] = time.monotonic()
    fetcher.delays["bpando.org"] = 300
    original_wait = fetcher.stop_event.wait
    monkeypatch.setattr(fetcher.stop_event, "wait", lambda timeout: entered.set() or original_wait(timeout))
    errors = []
    def request():
        try:
            fetcher._request("https://bpando.org/", 100)
        except FetchError as error:
            errors.append(error.code)
    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(2)
    fetcher.stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive() and errors == ["worker_stopped"]
    assert not reserved


def test_stop_waits_for_inflight_request_and_run_finalization(setup):
    store, plan, network, worker, _ = setup
    enable(store, plan)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def in_flight():
        entered.set()
        assert release.wait(5)
    network.after_image = in_flight
    worker.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=lambda: (worker.stop(), finished.set()))
    stopper.start()
    assert worker._stopping.wait(2)
    assert not finished.is_set()  # Caller still owns the worker process lock.
    assert store.list_plans(raw=True)[0]["active_run"]
    release.set()
    assert finished.wait(2)
    stopper.join(timeout=2)
    assert not worker._thread.is_alive() and not worker._heartbeat_thread.is_alive()
    run = store.list_plans()[0]["last_run"]
    assert run["status"] == "stopped" and run["imported"] == 1
    assert not store.list_plans(raw=True)[0]["active_run"]
