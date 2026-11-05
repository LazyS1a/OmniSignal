import httpx
import pytest

from omnisignal.workbench.source_adapters import (
    SOURCE_CONFIGS, SourceAdapterError, discover_projects, extract_project_metadata,
    fetch_pexels_photos, normalize_pinterest_sample, normalize_project,
    parse_pexels_photos, project_identity, source_catalog,
)


@pytest.mark.parametrize("source,url,image", [
    ("pinterest", "https://www.pinterest.com/pin/123/", "https://i.pinimg.com/736x/ab/cd/test.jpg"),
    ("zcool", "https://www.zcool.com.cn/work/ZABC==.html", "https://img.zcool.cn/community/test.jpg"),
    ("huaban", "https://huaban.com/pins/123/", "https://hbimg.huabanimg.com/test.webp"),
    ("behance", "https://www.behance.net/gallery/123/Project", "https://mir-s3-cdn-cf.behance.net/project_modules/fs/test.jpg"),
])
def test_project_detail_uses_own_metadata_not_navigation(source, url, image):
    raw = f'''<html><head><meta property="og:url" content="{url}">
      <meta property="og:title" content="Actual project"><meta property="og:image" content="{image}"></head>
      <nav><img src="{image.replace('test.', 'navigation.')}"></nav>
      <main><aside><img src="{image.replace('test.', 'recommended.')}"></aside></main></html>'''
    result = extract_project_metadata(source, raw, url)
    assert [x["url"] for x in result["candidates"]] == [image]
    assert result["text"] == ""
    assert set(result["usage_permissions"].values()) == {"unknown"}
    assert result["project_id"] == project_identity(source, url)["project_id"]


def test_behance_slug_changes_group_under_same_native_id():
    a = project_identity("behance", "https://www.behance.net/gallery/123/old-name")
    b = project_identity("behance", "https://behance.net/gallery/123/new-name?tracking=1")
    assert a["project_id"] == b["project_id"]
    assert "?" not in b["source_url"]


@pytest.mark.parametrize("bad", [
    "http://www.pinterest.com/pin/123/", "https://www.pinterest.com.evil.test/pin/123/",
    "https://user:pass@www.pinterest.com/pin/123/", "https://www.pinterest.com:8443/pin/123/",
    "https://127.0.0.1/pin/123/", "https://www.pinterest.com/login/",
])
def test_project_identity_refuses_wrong_scope(bad):
    with pytest.raises(SourceAdapterError):
        project_identity("pinterest", bad)


@pytest.mark.parametrize("bad", [
    "https://evil.test/pin.jpg", "https://i.pinimg.com.evil.test/pin.jpg", "file:///pin.jpg",
    "https://i.pinimg.com/pin.mp4", "https://i.pinimg.com/pin.svg", "https://i.pinimg.com/pin.gif",
    "https://secret@i.pinimg.com/pin.jpg", "https://i.pinimg.com:8000/pin.jpg",
])
def test_images_are_strictly_scoped_and_static(bad):
    with pytest.raises(SourceAdapterError, match="project_images_missing"):
        normalize_project("pinterest", {"project_url": "https://www.pinterest.com/pin/123/",
            "title": "Test", "images": [{"url": bad}]})


def test_discovery_ignores_navigation_and_deduplicates_native_ids():
    doc = '''<nav><a href="/gallery/999/Nav">Nav</a></nav><main>
      <a href="/gallery/123/a">A</a><a href="/gallery/123/b">B</a>
      <a href="https://evil.test/gallery/456/evil">C</a></main>'''
    assert discover_projects("behance", doc) == ["https://www.behance.net/gallery/123/a"]


def test_wrong_project_metadata_and_missing_metadata_fail_closed():
    with pytest.raises(SourceAdapterError, match="project_metadata_mismatch"):
        extract_project_metadata("pinterest", '<meta property="og:url" content="https://www.pinterest.com/pin/999/">',
                                 "https://www.pinterest.com/pin/123/")
    with pytest.raises(SourceAdapterError, match="project_title_missing"):
        extract_project_metadata("pinterest", '<main><img src="https://i.pinimg.com/nav.jpg"></main>',
                                 "https://www.pinterest.com/pin/123/")


def test_pinterest_existing_probe_contract_does_not_invent_original_or_dimensions():
    item = normalize_pinterest_sample({"pin_url": "https://www.pinterest.com/pin/123/",
        "image_url": "https://i.pinimg.com/474x/ab/test.jpg", "alt": "Packaging", "naturalWidth": 236})
    image = item["candidates"][0]
    assert image["url"] == "https://i.pinimg.com/474x/ab/test.jpg"
    assert image["declared_width"] is None
    assert image["image_basis"] == "listing-preview"


def photo_payload():
    return {"photos": [{"id": 123, "url": "https://www.pexels.com/photo/green-grass-123/",
        "width": 3000, "height": 2000, "photographer": "Photo author", "alt": "Grass",
        "src": {"original": "https://images.pexels.com/photos/123/pexels-photo-123.jpeg"}}]}


def test_pexels_photo_provenance():
    result = parse_pexels_photos(photo_payload())[0]
    assert result["native_id"] == "123"
    assert result["candidates"][0]["declared_width"] == 3000
    assert result["author"] == "Photo author"
    assert result["evidence_basis"] == "official-photo-api"
    assert result["usage_permissions"]["transmission"] == "unknown"


@pytest.mark.parametrize("payload", [{"videos": []}, {"photos": [], "videos": []}, {"photos": [{"id": True}]}])
def test_pexels_rejects_video_responses(payload):
    with pytest.raises(SourceAdapterError):
        parse_pexels_photos(payload)


def test_pexels_rejects_inconsistent_id():
    payload = photo_payload()
    payload["photos"][0]["id"] = 124
    with pytest.raises(SourceAdapterError, match="photo_identity_mismatch"):
        parse_pexels_photos(payload)


def test_official_client_reserves_before_single_request():
    events = []
    def handler(request):
        events.append("request")
        assert request.url.host == "api.pexels.com"
        assert request.url.path == "/v1/search"
        assert request.headers["Authorization"] == "fixture-key"
        assert "fixture-key" not in str(request.url)
        return httpx.Response(200, json=photo_payload())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_pexels_photos("packaging", api_key="fixture-key",
            reserve_request=lambda: events.append("reserve"), client=client)
    assert events == ["reserve", "request"]
    assert len(result) == 1


@pytest.mark.parametrize("status,error", [(302, "pexels_http_error"), (403, "pexels_authorization_required"), (429, "pexels_rate_limited")])
def test_official_client_does_not_retry_or_redirect(status, error):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://evil.test/"}, text="private response")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdapterError, match=f"^{error}$"):
            fetch_pexels_photos("test", api_key="fixture-key", reserve_request=lambda: None, client=client)
    assert len(requests) == 1


def test_missing_key_and_exhausted_budget_do_not_contact_network():
    def handler(request):
        pytest.fail("request must not happen")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceAdapterError, match="pexels_api_key_missing"):
            fetch_pexels_photos("test", api_key="", reserve_request=lambda: None, client=client)
        with pytest.raises(SourceAdapterError, match="request_budget_exhausted"):
            fetch_pexels_photos("test", api_key="fixture", reserve_request=lambda: False, client=client)


def test_catalog_is_explicitly_unverified_and_returns_independent_lists():
    catalog = source_catalog()
    assert len(catalog) == 5
    assert all(not row["verified"] for row in catalog)
    catalog[0]["hosts"].append("evil.test")
    assert "evil.test" not in SOURCE_CONFIGS["pinterest"]["hosts"]
