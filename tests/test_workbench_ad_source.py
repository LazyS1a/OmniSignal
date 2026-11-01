import io

import pytest
from PIL import Image

from omnisignal.workbench import ad_source
from omnisignal.workbench.collection import WorkbenchCollector
from omnisignal.workbench.store import Store, sources


URL = "https://adsofbrands.net/en/ads/synthetic-outdoor-campaign/12345"
IMAGE = "https://adsofbrands.net/images/commercial_big/1/123456789.jpeg"


def page(image=IMAGE, media="Outdoor"):
    return f"""<html><meta property="og:image" content="1">
    <div id="com-left"><div class="com-media"><div class="print-image">
    <a href="https://adsofbrands.net/images/commercial_origin/1/123456789.jpeg"><img src="{image}"></a>
    </div></div><div class="prev-next"><img src="https://adsofbrands.net/images/commercial/1/999999.jpeg"></div></div>
    <div id="com-right"><h1>Synthetic Brand: Outdoor campaign</h1>
    <div class="media-type"><a><span>{media}</span></a></div>
    <div class="brand"><img src="https://adsofbrands.net/images/brand/1/111111.jpeg"></div>
    <div class="country"><a>Example country</a></div><div class="industry"><a>Example industry</a></div>
    <div class="description">This synthetic campaign uses a short headline and a clear visual contrast for an outdoor placement.</div>
    <div id="credits-box"><div class="credit"><p class="position">Advertising Agency:</p><span>Example agency</span></div>
    <div class="credit"><p class="position">Date:</p><span>October, 2026</span></div></div></div>
    <div class="recommendations"><img src="https://adsofbrands.net/images/commercial_big/1/999999.jpeg"></div></html>""".encode()


def test_discovery_only_explicit_campaign_cards():
    document = f"""<a class="item" href="{URL}">one</a><a class="item" href="{URL}?tracking=1">same</a>
    <a class="item" href="/en/ads/">directory</a><a class="item" href="https://evil.invalid/en/ads/example/123">foreign</a>
    <a class="item" href="/en/news/example/123">news</a><a href="/en/ads/navigation/456">navigation</a>"""
    assert ad_source.discover(document) == [URL]


def test_explicit_media_container_excludes_logos_related_and_broken_og():
    article = ad_source.extract_article(page(), URL)
    assert article["candidates"] == [{"url": IMAGE}]
    assert article["source_facts"] == {"media_type": "Outdoor", "country": "Example country", "industry": "Example industry",
                                       "advertising_agency": "Example agency", "date": "October, 2026"}
    assert "advertising_agency: Example agency" in article["text"]
    assert article["published_at"] is None  # The source only supplies a month.
    assert "purposes" not in article and "permissions" not in article


@pytest.mark.parametrize("url", ["https://adsofbrands.net/en/ads/", "https://foreign.invalid/en/ads/slug/123", "https://u:p@adsofbrands.net/en/ads/slug/123"])
def test_rejects_outside_project_scope(url):
    with pytest.raises(ValueError, match="outside_ad_project_scope"):
        ad_source.extract_article(page(), url)


@pytest.mark.parametrize("url", ["https://cdn.invalid/images/commercial_big/1/123.jpeg", "https://adsofbrands.net/images/brand/1/123.jpeg", IMAGE + "?token=example", "http://adsofbrands.net/images/commercial_big/1/123.jpeg"])
def test_rejects_unverified_image_hosts_and_non_project_paths(url):
    with pytest.raises(ValueError, match="ad_project_images_missing"):
        ad_source.extract_article(page(image=url), URL)


def test_video_and_missing_structure_fail_closed():
    with pytest.raises(ValueError, match="unsupported_ad_media_type"):
        ad_source.extract_article(page(media="Film"), URL)
    with pytest.raises(ValueError, match="ad_project_structure_changed"):
        ad_source.extract_article("<h1>Title only</h1>", URL)


@pytest.mark.parametrize("embedded", [
    '<iframe src="https://www.youtube.com/embed/synthetic"></iframe>',
    '<video src="https://adsofbrands.net/synthetic.mp4"></video>',
])
def test_embedded_only_media_is_distinct_from_a_changed_page_structure(embedded):
    document = page().decode().replace(
        f'<div class="print-image">\n    <a href="https://adsofbrands.net/images/commercial_origin/1/123456789.jpeg"><img src="{IMAGE}"></a>\n    </div>',
        embedded)
    with pytest.raises(ValueError, match="^ad_project_non_static_media$"):
        ad_source.extract_article(document, URL)


def test_static_media_with_video_still_uses_only_verified_project_images():
    document = page().decode().replace('<div class="com-media">',
        '<div class="com-media"><iframe src="https://www.youtube.com/embed/synthetic"></iframe>')
    assert ad_source.extract_article(document, URL)["candidates"] == [{"url": IMAGE}]


def test_missing_media_container_is_not_reported_as_known_embedded_media():
    document = page().decode().replace('class="com-media"', 'class="unknown-layout"')
    with pytest.raises(ValueError, match="^ad_project_structure_changed$"):
        ad_source.extract_article(document, URL)


def carousel_page(image=IMAGE):
    """Synthetic text; media DOM matches the public 2026-11-01 sample.

    Observed inside #com-left .com-media: .print-images-carousel directly
    contains .print-image-item, which directly contains a commercial_big img.
    No source prose or image is copied into the fixture.
    """
    old = f'<div class="print-image">\n    <a href="https://adsofbrands.net/images/commercial_origin/1/123456789.jpeg"><img src="{image}"></a>\n    </div>'
    new = f'<div class="print-images-carousel "><div class="print-image-item"><img src="{image}"></div></div>'
    document = page(image=image).decode()
    assert old in document
    return document.replace(old, new)


def test_observed_carousel_static_media_is_supported_without_accepting_related_cards():
    document = carousel_page() + f'''<div id="similar-ads"><div class="print-images-carousel">
    <div class="print-image-item"><img src="https://adsofbrands.net/images/commercial_big/1/999999.jpeg"></div>
    </div></div>'''
    assert ad_source.extract_article(document, URL)["candidates"] == [{"url": IMAGE}]


@pytest.mark.parametrize("url", [
    "https://cdn.invalid/images/commercial_big/1/123.jpeg",
    "https://adsofbrands.net/images/brand/1/123.jpeg",
    IMAGE + "?token=synthetic", "http://adsofbrands.net/images/commercial_big/1/123.jpeg",
])
def test_carousel_retains_the_existing_image_origin_and_path_boundaries(url):
    with pytest.raises(ValueError, match="^ad_project_images_missing$"):
        ad_source.extract_article(carousel_page(image=url), URL)


def test_unverified_image_item_outside_carousel_is_not_a_new_fallback():
    document = carousel_page().replace('class="print-images-carousel "', 'class="unknown-wrapper"')
    with pytest.raises(ValueError, match="^ad_project_structure_changed$"):
        ad_source.extract_article(document, URL)


def test_carousel_keeps_image_order_deduplicates_and_retains_six_candidate_limit():
    images = [IMAGE, IMAGE] + [f"https://adsofbrands.net/images/commercial_big/1/{123456790+i}.jpeg" for i in range(7)]
    original = f'<div class="print-image-item"><img src="{IMAGE}"></div>'
    items = ''.join(f'<div class="print-image-item"><img src="{image}"></div>' for image in images)
    document = carousel_page().replace(original, items)
    assert ad_source.extract_article(document, URL)["candidates"] == [
        {"url": image} for image in list(dict.fromkeys(images))[:6]]


def test_advertising_source_uses_same_pending_budgeted_import_flow(tmp_path):
    store = Store(tmp_path)
    plan = store.create_plan({"name": "Outdoor", "source_id": "adsofbrands", "request_limit": 10}, "editor")
    store.control_plan(plan["id"], {"action": "resume", "expected_revision": 1}, "editor")
    assert next(s for s in sources() if s["id"] == "adsofbrands")["category"] == "advertising"
    calls = []
    def factory(hosts, reserve):
        assert hosts == ["adsofbrands.net"]
        class Fetcher:
            def get(self, url, *, image=False):
                assert reserve()
                calls.append(url)
                if url == ad_source.CONFIG["listing"]:
                    return f'<a class="item" href="{URL}">campaign</a>'.encode()
                if not image:
                    return page()
                output = io.BytesIO()
                Image.new("RGB", (500, 700), "red").save(output, "JPEG")
                return output.getvalue()
        return Fetcher()
    result = WorkbenchCollector(store, fetcher_factory=factory).sweep()[0]
    assert result["status"] == "ok" and result["imported"] == 1
    assert calls == [ad_source.CONFIG["listing"], URL, IMAGE]
    asset = store.list_assets(status="pending")["items"][0]
    assert asset["source_id"] == "adsofbrands" and asset["purposes"] == []
    assert asset["kind"] == "uncertain" and asset["permissions"]["export"] == "unknown"
