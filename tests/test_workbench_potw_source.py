"""Offline tests for the Packaging of the World adapter and its transport.

Fixtures are synthetic and shaped after the live DOM verified on 2026-10-23;
no network access and no real project imagery is used.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from omnisignal.workbench import potw_source
from omnisignal.workbench.collection import SOURCE_CONFIGS, WorkbenchCollector
from omnisignal.workbench.potw_transport import (
    looks_like_interstitial,
    solve_interstitial,
)
from omnisignal.workbench.store import Store, sources


PROJECT = "https://packagingoftheworld.com/2026/10/example-project.html"
IMAGE = "https://packagingoftheworld.com/wp-content/uploads/2026/10/example-1-scaled.jpeg"
CDN_IMAGE = "https://i0.wp.com/packagingoftheworld.com/wp-content/uploads/2026/10/example-2-scaled.jpeg?fit=2560%2C1534&ssl=1"


def listing_page(*hrefs: str) -> bytes:
    anchors = "".join(f'<a href="{h}">card</a>' for h in hrefs)
    return f"""<html><body><div class="row">{anchors}</div>
    <nav><a href="/packages/page/2/">2</a><a href="/packages/page/676/">676</a></nav>
    </body></html>""".encode()


def project_page(*, images=None, media_title="Example Project", body=None) -> bytes:
    images = images if images is not None else [IMAGE, CDN_IMAGE]
    figures = "".join(f'<figure><a href="{u.split("?")[0]}"><img src="{u}" alt=""></a></figure>' for u in images)
    paragraphs = body or [
        "This synthetic packaging project uses a bold typographic lockup and a warm colour field for shelf impact.",
        "The system repeats across three sizes so the range reads as one family on a busy retail shelf.",
    ]
    body_html = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"""<html><head>
    <meta property="article:published_time" content="2026-10-23T09:00:00+08:00">
    <title>{media_title}</title></head><body>
    <nav><img src="https://packagingoftheworld.com/wp-content/uploads/2021/09/Logo-desktop.svg" alt="logo"></nav>
    <article class="post-1 post type-post category-beverages location-us material-glass project-type-produced">
      <div class="entry-header"><h1 class="entry-title">{media_title}</h1></div>
      <div class="entry-meta"><span class="submitted"><a>Example Studio</a></span></div>
      <div class="entry-content">{body_html}{figures}
        <img src="https://packagingoftheworld.com/wp-content/uploads/avatars/1/avatar.png" alt="avatar">
      </div>
    </article>
    <div class="related-project"><img src="https://packagingoftheworld.com/wp-content/uploads/2026/10/other-project.jpeg"></div>
    <img src="https://cdn.invalid/evil.jpeg">
    </body></html>""".encode()


def jpeg(colour="red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (600, 800), colour).save(output, "JPEG")
    return output.getvalue()


# -- discovery ------------------------------------------------------------

def test_discovery_keeps_only_real_project_paths():
    document = listing_page(
        PROJECT,
        PROJECT,                                  # duplicate
        "/2026/10/relative-post.html",            # resolves to a project
        "/packages/page/2/",                      # navigation
        "/category/beverages",                    # taxonomy
        "https://evil.invalid/2026/10/post.html",  # foreign host
        "https://packagingoftheworld.com/about.html",
    )
    urls = potw_source.discover(document, "https://packagingoftheworld.com/")
    assert urls == [
        PROJECT,
        "https://packagingoftheworld.com/2026/10/relative-post.html",
    ]


def test_discovery_reads_highest_archive_page():
    assert potw_source.discover_total_pages(listing_page(PROJECT)) == 676


@pytest.mark.parametrize("url", [
    "https://packagingoftheworld.com/packages/page/2/",
    "https://packagingoftheworld.com/category/beverages",
    "http://packagingoftheworld.com/2026/10/post.html",
    "https://u:p@packagingoftheworld.com/2026/10/post.html",
])
def test_is_project_rejects_non_project_urls(url):
    assert potw_source.is_project(url) is False


# -- extraction -----------------------------------------------------------

def test_extract_keeps_own_media_and_drops_furniture():
    article = potw_source.extract_article(project_page(), PROJECT)
    assert article["title"] == "Example Project"
    # The Jetpack CDN wrapper is normalised to the origin URL, so both survive
    # exactly once each; logos, avatars, related cards and foreign hosts do not.
    assert [c["url"] for c in article["candidates"]] == [
        IMAGE,
        "https://packagingoftheworld.com/wp-content/uploads/2026/10/example-2-scaled.jpeg",
    ]
    assert article["published_at"] == "2026-10-23"
    assert article["date_basis"] == "article_published_time"
    assert "bold typographic lockup" in article["text"]


def test_extract_reports_taxonomy_facts():
    article = potw_source.extract_article(project_page(), PROJECT)
    facts = article["source_facts"]
    assert facts["category"] == ["beverages"]
    assert facts["location"] == ["us"]
    assert facts["material"] == ["glass"]
    assert facts["project_type"] == ["produced"]
    assert facts["submitted_by"] == ["Example Studio"]
    assert "category: beverages" in article["text"]


def test_extract_fails_closed_on_structure_drift():
    with pytest.raises(ValueError, match="project_structure_changed"):
        potw_source.extract_article(b"<h1>Title only</h1>", PROJECT)


def test_extract_rejects_page_without_project_media():
    page = project_page(images=[])
    with pytest.raises(ValueError, match="project_images_missing"):
        potw_source.extract_article(page, PROJECT)


def test_extract_rejects_outside_project_scope():
    with pytest.raises(ValueError, match="outside_project_scope"):
        potw_source.extract_article(project_page(), "https://packagingoftheworld.com/category/beverages")


# -- interstitial ---------------------------------------------------------

def _challenge_script() -> str:
    # Mirrors the live Sucuri payload shape: literals + fromCharCode building a
    # value, then a document.cookie assignment using split name literals.
    return (
        "p='0' + String.fromCharCode(57) + \"f\" + '2';"
        "document.cookie='s'+'u'+'c'+'u'+'r'+'i'+'_'+'c'+'l'+'o'+'u'+'d'+'p'+'r'+'o'+'x'+'y'"
        "+'_'+'u'+'u'+'i'+'d'+'_'+'a'+'b'+'c'+\"=\" + p + ';path=/;max-age=86400;SameSite=Lax;Secure';"
        "location.reload();"
    )


def _challenge_page() -> bytes:
    import base64

    encoded = base64.b64encode(_challenge_script().encode()).decode()
    return (
        "<html><title>You are being redirected...</title>"
        f"<script>var s={{}},S='{encoded}';</script></html>"
    ).encode()


def test_interstitial_is_detected():
    assert looks_like_interstitial(_challenge_page()) is True
    assert looks_like_interstitial(b"<html>normal page</html>") is False


def test_interstitial_cookie_is_rebuilt_without_eval():
    assert solve_interstitial(_challenge_page()) == ("sucuri_cloudproxy_uuid_abc", "09f2")


def test_interstitial_without_payload_returns_none():
    assert solve_interstitial(b"<html>no script here</html>") is None


def test_fetch_falls_back_to_urllib_without_curl(monkeypatch):
    """The production container has no curl; the transport must still work.

    Regression guard: an earlier revision hard-depended on the curl binary and
    failed with network_error on a container that only ships Python.
    """
    import omnisignal.workbench.potw_transport as transport

    calls = []

    def fake_urllib(url, user_agent, cookies, timeout, *, attempts=3, backoff=0.0):
        calls.append(("urllib", url))
        return 200, b"<html>ok</html>", "text/html"

    def fake_curl(*args, **kwargs):  # must not be used when curl is absent
        calls.append(("curl", "unexpected"))
        raise AssertionError("curl must not be called when unavailable")

    monkeypatch.setattr(transport, "_curl_present", lambda: False)
    monkeypatch.setattr(transport, "_urllib_get", fake_urllib)
    monkeypatch.setattr(transport, "_curl_get", fake_curl)

    status, body, content_type = transport._fetch("https://packagingoftheworld.com/", "UA", {}, 30)
    assert (status, body, content_type) == (200, b"<html>ok</html>", "text/html")
    assert calls == [("urllib", "https://packagingoftheworld.com/")]


def test_fetch_prefers_curl_when_available(monkeypatch):
    import omnisignal.workbench.potw_transport as transport

    # _fetch looks up _curl_get at module scope, so patch that name directly
    # and let _curl_present report the real curl availability.
    monkeypatch.setattr(transport, "_curl_present", lambda: True)
    monkeypatch.setattr(transport, "_curl_get",
                        lambda *a, **k: (200, b"curl", "text/html"))
    status, body, _ = transport._fetch("https://packagingoftheworld.com/", "UA", {}, 30)
    assert (status, body) == (200, b"curl")


# -- integration ----------------------------------------------------------

def test_potw_source_is_registered_and_verified():
    entry = next(s for s in sources() if s["id"] == "potw")
    assert entry["verified"] is True
    assert entry["category"] == "supplemental"
    assert SOURCE_CONFIGS["potw"]["crawl_delay"] == 60


def test_production_path_selects_potw_transport():
    """No fetcher_factory passed (the app.py path) must still get POTWTransport.

    Regression guard: the transport is useless if only tests wire it up.
    """
    from omnisignal.workbench.collection import BudgetedFetcher, source_fetcher
    from omnisignal.workbench.potw_transport import POTWTransport

    reserve = lambda: True
    potw = source_fetcher("potw", SOURCE_CONFIGS["potw"], reserve)
    assert isinstance(potw, POTWTransport)
    assert potw.hosts == {"packagingoftheworld.com"}

    plain = source_fetcher("bpando", SOURCE_CONFIGS["bpando"], reserve)
    assert isinstance(plain, BudgetedFetcher)

    # The collector itself, with no factory, defers to the same chooser.
    collector = WorkbenchCollector(Store.__new__(Store))  # avoid touching disk
    fetcher = collector._build_fetcher("potw", SOURCE_CONFIGS["potw"], reserve)
    assert isinstance(fetcher, POTWTransport)


def test_potw_collection_uses_same_pending_budgeted_flow(tmp_path):
    store = Store(tmp_path)
    plan = store.create_plan({"name": "POTW", "source_id": "potw", "request_limit": 10}, "editor")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": 1}, "editor")
    calls = []

    class Fetcher:
        def __init__(self, hosts, reserve):
            assert "packagingoftheworld.com" in hosts
            self.reserve = reserve
            self.stop_event = None

        def get(self, url, *, image=False):
            assert self.reserve()
            calls.append(url)
            if url == potw_source.CONFIG["listing"]:
                return listing_page(PROJECT)
            if not image:
                return project_page()
            # Distinct bytes per image: content hashing de-duplicates identical
            # payloads, which is the storage contract being exercised.
            return jpeg("red" if "example-1" in url else "blue")

    result = WorkbenchCollector(store, fetcher_factory=Fetcher).sweep()[0]
    assert result["status"] == "ok"
    assert result["imported"] == 2  # two distinct project images

    # Listing first, then the project, then its images; never a navigation URL.
    assert calls[0] == potw_source.CONFIG["listing"]
    assert calls[1] == PROJECT
    assert all("packages/page" not in c for c in calls)

    pending = store.list_assets(status="pending")["items"]
    assert len(pending) == 2
    assert all(a["source_id"] == "potw" for a in pending)
    # Collection can never grant permission or an advertising purpose.
    assert all(a["purposes"] == [] for a in pending)
    assert all(a["kind"] == "uncertain" for a in pending)
    assert all(a["permissions"]["export"] == "unknown" for a in pending)


def test_potw_second_run_skips_known_projects(tmp_path):
    store = Store(tmp_path)
    plan = store.create_plan({"name": "POTW", "source_id": "potw", "request_limit": 20}, "editor")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": 1}, "editor")
    fetched: list[str] = []

    class Fetcher:
        def __init__(self, hosts, reserve):
            self.reserve = reserve
            self.stop_event = None

        def get(self, url, *, image=False):
            assert self.reserve()
            fetched.append(url)
            if url == potw_source.CONFIG["listing"]:
                return listing_page(PROJECT)
            if not image:
                return project_page()
            return jpeg()

    collector = WorkbenchCollector(store, fetcher_factory=Fetcher)
    collector.sweep()
    first_project_fetches = fetched.count(PROJECT)
    assert first_project_fetches == 1

    fetched.clear()
    # Plans schedule on an interval; force one run to prove the project itself
    # is recognised as already collected rather than re-downloaded.
    collector.run(plan["id"], force=True)
    assert fetched == [potw_source.CONFIG["listing"]]
