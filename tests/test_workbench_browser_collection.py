"""Offline transport contract: no live site, browser profile or model calls."""
import hashlib
import io
import json

from PIL import Image
import pytest

from omnisignal.workbench.browser_collection import (
    BrowserCollectionConfig, BrowserCollectionError, PinterestBrowserProvider,
    PROFILE_MARKER, SAFE_STATES, prepare_profile,
)
from omnisignal.workbench.store import Store


HOME = "https://www.pinterest.com/search/pins/?q=design"
PIN = "https://www.pinterest.com/pin/123456789/"
PIN2 = "https://www.pinterest.com/pin/987654321/"
SMALL = "https://i.pinimg.com/236x/aa/bb/cc/sample.jpg"
LARGE = "https://i.pinimg.com/736x/aa/bb/cc/sample.jpg"
NEXT = HOME + "&page=2"


def listing(**kwargs):
    return dict(url=HOME, cards=[{"project_url": PIN}, {"project_url": PIN2}],
                next_url=None, **kwargs)


def detail(**changes):
    value = {"url": PIN, "title": "Observed design", "description": "Public caption",
             "canonical_urls": [PIN], "detail_images": [{"alt": "Observed poster", "choices": [
                 {"url": SMALL, "width": 236, "height": 300},
                 {"url": LARGE, "width": 736, "height": None}]}]}
    value.update(changes)
    return value


class FakeBrowser:
    def __init__(self, pages, guard):
        self.pages, self.guard = pages, guard
        self.url, self.round, self.closed, self.scrolls = None, 0, False, 0
        self.navigations = []

    def navigate(self, url):
        assert self.guard(url, "document", "GET")
        self.navigations.append(url)
        self.url, self.round = url, 0
        return self.pages[url].get("http_status", 200) if isinstance(self.pages[url], dict) else 200

    def snapshot(self):
        page = self.pages[self.url]
        return page[min(self.round, len(page) - 1)] if isinstance(page, list) else page

    def scroll(self):
        self.scrolls += 1
        self.round += 1

    def close(self):
        self.closed = True


def make(tmp_path, pages=None, **kwargs):
    calls = {"reserve": 0, "policy": [], "browsers": [], "images": []}
    def reserve():
        calls["reserve"] += 1
        return True
    def policy(url):
        calls["policy"].append(url)
        return True
    def factory(profile, guard, *, headless):
        browser = FakeBrowser(pages or {HOME: listing(), PIN: detail()}, guard)
        calls["browsers"].append(browser)
        return browser
    config = kwargs.pop("config", BrowserCollectionConfig(tmp_path / "dedicated", HOME, max_scrolls=0))
    provider = PinterestBrowserProvider(config, kwargs.pop("reserve_request", reserve),
        kwargs.pop("access_policy", policy), browser_factory=kwargs.pop("browser_factory", factory),
        resolve_host=kwargs.pop("resolve_host", lambda host: ["93.184.216.34"]), **kwargs)
    return provider, calls


def fails(code, operation):
    with pytest.raises(BrowserCollectionError) as error:
        operation()
    assert error.value.code == code
    assert str(error.value) == code


def test_dom_to_original_bytes_and_isolated_store_permissions(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (736, 900), "green").save(stream, "JPEG")
    original = stream.getvalue()
    fetched = []
    provider, calls = make(tmp_path, image_fetcher=lambda url: fetched.append(url) or original)
    assert provider.discover()["project_urls"] == [PIN, PIN2]
    result = provider.detail(PIN)
    assert result["snapshot"]["images"][0]["url"] == LARGE
    assert result["snapshot"]["images"][0]["height"] is None
    content = provider.get_image(LARGE)
    assert content == original and fetched == [LARGE]
    store = Store(tmp_path / "isolated")
    article = result["article"]
    asset = store.import_asset({"title": article["title"], "source_id": "pinterest",
        "source_url": PIN, "project_id": article["project_id"], "source_image_url": LARGE}, content,
        actor="collector")
    assert asset["sha256"] == hashlib.sha256(original).hexdigest()
    assert (store.images / asset["image_file"]).read_bytes() == original
    assert asset["permissions"] == {"display": "unknown", "analysis": "unknown", "export": "unknown"}
    assert asset["status"] == "pending" and asset["analysis_status"] == "not_analyzed"
    assert asset["project_id"] == "pinterest-" + hashlib.sha256(b"123456789").hexdigest()[:20]
    assert provider.status()["operationally_verified"] is False
    provider.close()
    assert calls["browsers"][0].closed


def test_only_observed_next_link_is_resumable(tmp_path):
    first = listing()
    first["next_url"] = NEXT
    second = listing()
    second["url"] = NEXT
    provider, calls = make(tmp_path, pages={HOME: first, NEXT: second})
    first_result = provider.discover()
    assert first_result["exhausted"] is False
    assert first_result["pagination_state"] == "observed_next_link"
    result = provider.discover(resume_cursor=first_result["resume_cursor"])
    assert calls["browsers"][0].navigations == [HOME, NEXT]
    assert result["exhausted"] is None and result["resume_cursor"] is None
    assert result["pagination_state"] == "pagination_unverified"
    provider.close()


def test_limited_discovery_does_not_skip_remaining_current_page(tmp_path):
    page = listing()
    page["next_url"] = NEXT
    provider, _ = make(tmp_path, pages={HOME: page})
    result = provider.discover(limit=1)
    assert result["discovery_truncated"] is True
    assert result["next_url"] is None and result["resume_cursor"] is None
    provider.close()


def test_bounded_scroll_is_not_persistent_pagination(tmp_path):
    first = listing()
    first["cards"] = [{"project_url": PIN}]
    provider, calls = make(tmp_path, pages={HOME: [first, listing()]},
        config=BrowserCollectionConfig(tmp_path / "profile", HOME, max_scrolls=3))
    result = provider.discover()
    assert result["project_urls"] == [PIN, PIN2]
    assert result["pagination_state"] == "pagination_unverified"
    assert result["exhausted"] is None and calls["browsers"][0].scrolls == 3
    provider.close()


@pytest.mark.parametrize("changes,code", [
    ({"challenge": True}, "challenge"), ({"login_required": True}, "login_required"),
    ({"http_status": 403}, "access_denied"), ({"cards": []}, "structure_unavailable"),
])
def test_unavailable_session_stops_without_retry_or_fallback(tmp_path, changes, code):
    page = listing()
    page.update(changes)
    provider, calls = make(tmp_path, pages={HOME: page})
    fails(code, provider.discover)
    fails(code, lambda: provider.detail(PIN))
    assert calls["browsers"][0].navigations == [HOME]
    assert calls["browsers"][0].scrolls == 0
    provider.close()
    assert provider.status()["state"] == code


@pytest.mark.parametrize("canonical", [PIN2, "https://es.pinterest.com/pin/987654321/", "https://evil.example/pin/123456789/"])
def test_cross_pin_canonical_never_enters_image_pipeline(tmp_path, canonical):
    provider, _ = make(tmp_path, pages={PIN: detail(canonical_urls=[canonical])})
    fails("identity_conflict", lambda: provider.detail(PIN))
    assert provider.status()["observed_image_count"] == 0
    provider.close()


def test_unrelated_recommendation_images_are_not_detail_candidates(tmp_path):
    provider, _ = make(tmp_path, pages={PIN: detail(detail_images=[], cards=[{"project_url": PIN2, "image": LARGE}])})
    fails("image_unavailable", lambda: provider.detail(PIN))
    provider.close()


def test_guessed_original_url_is_not_downloadable(tmp_path):
    provider, _ = make(tmp_path)
    provider.detail(PIN)
    fails("image_not_observed", lambda: provider.get_image(LARGE.replace("736x", "originals")))
    provider.close()


@pytest.mark.parametrize("policy,code", [(None, "policy_not_configured"), (lambda url: False, "policy_denied")])
def test_robots_and_prior_denial_gate_precedes_profile_and_launch(tmp_path, policy, code):
    provider, calls = make(tmp_path, access_policy=policy)
    fails(code, provider.open)
    assert not calls["browsers"] and calls["reserve"] == 0
    assert not (tmp_path / "dedicated").exists()


def test_refuse_existing_unmarked_profile_without_reading_cookies(tmp_path):
    path = tmp_path / "old-profile"
    path.mkdir()
    sentinel = path / "Cookies"
    sentinel.write_bytes(b"do not read or import")
    fails("profile_unmanaged", lambda: prepare_profile(path))
    assert not (path / PROFILE_MARKER).exists()
    assert sentinel.read_bytes() == b"do not read or import"


def test_own_dedicated_profile_can_reopen_and_lock_is_released(tmp_path):
    provider, _ = make(tmp_path)
    provider.open()
    provider.close()
    provider.open()
    assert provider.status()["state"] == "ready"
    assert json.loads((tmp_path / "dedicated" / PROFILE_MARKER).read_text())["purpose"] == "pinterest-collection"
    provider.close()


def test_every_allowed_subresource_reserves_budget_unknown_hosts_block(tmp_path):
    provider, calls = make(tmp_path)
    provider.open()
    assert provider._request("https://s.pinimg.com/webapp/observed.js", "script") is True
    assert provider._request("https://tracker.example/beacon", "image") is False
    assert provider._request("http://127.0.0.1/private", "image") is False
    assert provider._request("https://www.pinterest.com/unknown-api/", "fetch") is False
    assert calls["reserve"] == 1
    assert provider.status()["blocked_subresources"] == 3
    provider.close()


@pytest.mark.parametrize("url,kind,method", [(HOME, "document", "POST"),
    ("https://www.pinterest.com/settings/account/", "document", "GET"),
    ("https://www.pinterest.com/login/", "document", "GET")])
def test_navigation_scope_has_no_login_or_mutation(tmp_path, url, kind, method):
    provider, calls = make(tmp_path)
    provider.open()
    fails("login_required" if "/login/" in url else "outside_scope", lambda: provider._request(url, kind, method))
    assert calls["reserve"] == 0
    provider.close()


def test_public_hostname_private_dns_stops_before_transport(tmp_path):
    provider, calls = make(tmp_path, resolve_host=lambda host: ["127.0.0.1"])
    fails("non_public_address", provider.discover)
    assert calls["reserve"] == 0
    provider.close()


def test_request_budget_and_per_run_ceiling(tmp_path):
    provider, _ = make(tmp_path, reserve_request=lambda: False)
    fails("budget_exhausted", provider.discover)
    provider.close()
    provider, _ = make(tmp_path, config=BrowserCollectionConfig(tmp_path / "another", HOME, max_requests=1))
    provider.discover()
    fails("request_limit", lambda: provider.detail(PIN))
    provider.close()


def test_time_bound_and_sanitized_error_do_not_leak_profile_or_exception(tmp_path):
    now = [0]
    provider, _ = make(tmp_path, clock=lambda: now[0])
    provider.open()
    now[0] = 121
    fails("time_limit", provider.discover)
    assert str(tmp_path) not in json.dumps(provider.status())
    assert provider.status()["state"] in SAFE_STATES
    provider.close()
    provider, _ = make(tmp_path, browser_factory=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret")))
    fails("browser_unavailable", provider.open)


def test_invalid_cursor_cannot_navigate_arbitrary_host(tmp_path):
    provider, calls = make(tmp_path)
    fails("invalid_cursor", lambda: provider.discover(resume_cursor={"schema_version": 1,
        "url": "https://localhost/private", "discovered_from": HOME}))
    assert calls["browsers"] == []


def test_invalid_media_bytes_never_leave_provider(tmp_path):
    provider, _ = make(tmp_path, image_fetcher=lambda url: b"not an image")
    provider.detail(PIN)
    fails("image_unavailable", lambda: provider.get_image(LARGE))
    provider.close()


@pytest.mark.parametrize("code", ["robots_denied", "paused", "worker_stopped", "request_blocked"])
def test_known_policy_and_budget_errors_keep_original_code(tmp_path, code):
    from omnisignal.knowledge.design_collection import FetchError
    def reject(*args):
        raise FetchError(code)
    kwargs = {"access_policy": reject} if code == "robots_denied" else {"reserve_request": reject}
    provider, _ = make(tmp_path, **kwargs)
    fails(code, provider.discover)
    provider.close()


@pytest.mark.parametrize("changes", [{"max_scrolls": 20}, {"max_seconds": 0},
    {"profile_dir": "relative"}, {"listing_url": "https://localhost/"},
    {"resource_paths": (("www.pinterest.com", "/"),)}])
def test_dataclass_rejects_bad_configuration_before_readiness(tmp_path, changes):
    args = dict(profile_dir=tmp_path / "profile", listing_url=HOME)
    args.update(changes)
    fails("invalid_config", lambda: BrowserCollectionConfig(**args))


def test_policy_wait_counts_against_runtime_before_profile_or_launch(tmp_path):
    now = [0]
    def policy(url):
        now[0] += 150
        return True
    provider, calls = make(tmp_path, clock=lambda: now[0], access_policy=policy)
    fails("time_limit", provider.open)
    assert not calls["browsers"] and not (tmp_path / "dedicated").exists()


def test_marked_profile_cannot_redirect_chromium_into_other_directory(tmp_path):
    root = prepare_profile(tmp_path / "owned")
    other = tmp_path / "other"
    other.mkdir()
    try:
        (root / "chromium").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this Windows host")
    fails("profile_unmanaged", lambda: prepare_profile(root))


def test_chromium_file_is_not_accepted_as_profile(tmp_path):
    root = prepare_profile(tmp_path / "owned")
    (root / "chromium").write_text("invalid", encoding="utf-8")
    fails("profile_unmanaged", lambda: prepare_profile(root))
