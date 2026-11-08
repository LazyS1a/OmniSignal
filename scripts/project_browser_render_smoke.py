"""Offline Chromium fixture probe; run with network disabled and no business mounts.

No real source navigation: every request is fulfilled from synthetic data, while
the provider's normal request guard and Chromium rendering still execute.
"""
import io
import json
import tempfile
import time
from pathlib import Path

from PIL import Image
from omnisignal.workbench.project_browser import (
    ProjectBrowserConfig, ProjectBrowserProvider, _ProjectBrowser,
)


def main():
    fixture_sites = {
        "huaban": ("https://huaban.com/", "pins/10001/", "hbimg.huabanimg.com"),
        "zcool": ("https://www.zcool.com.cn/", "work/fixture123.html", "img.zcool.cn"),
        "behance": ("https://www.behance.net/", "gallery/10001/Fixture", "mir-s3-cdn-cf.behance.net"),
    }
    output = io.BytesIO()
    Image.new("RGB", (640, 720), (40, 100, 170)).save(output, "PNG")
    results = []
    with tempfile.TemporaryDirectory(prefix="omni-render-fixture-") as temporary:
        for source, (origin, detail_path, image_host) in fixture_sites.items():
            detail_url = origin + detail_path
            image_url = f"https://{image_host}/fixture.png"
            optional_url = f"https://{image_host}/optional.png"
            delayed = "<main id='work'></main><script>setTimeout(()=>{document.querySelector('#work').innerHTML="
            delayed += json.dumps(f'<a href="{detail_url}">Synthetic project</a>') + ";},1000)</script>"
            detail = '<style>.hidden-login{display:none}</style><input class="hidden-login" type="password">'
            detail += f'<img src="{optional_url}"><main><h1>Synthetic project</h1></main>'
            detail += '<script>setTimeout(()=>{document.head.insertAdjacentHTML("beforeend",'
            detail += json.dumps(f'<meta property="og:title" content="Synthetic project"><meta property="og:url" content="{detail_url}"><meta property="og:image" content="{image_url}">')
            detail += ');},1000)</script>'
            fixture_documents = {origin: delayed, detail_url: detail}
            seen = {"network_attempts": 0, "optional_403": 0}

            class FixtureBrowser(_ProjectBrowser):
                def __init__(self, profile, before_request, *, headless=True):
                    super().__init__(profile, before_request, headless=headless)
                    self.context.unroute("**/*")

                    def fulfill(route):
                        request = route.request
                        kind = "document" if request.is_navigation_request() else request.resource_type
                        if not before_request(request.url, kind, request.method):
                            route.abort()
                            return
                        seen["network_attempts"] += 1
                        if request.url in fixture_documents:
                            route.fulfill(status=200, content_type="text/html", body=fixture_documents[request.url])
                        elif request.url == optional_url:
                            seen["optional_403"] += 1
                            route.fulfill(status=403, content_type="text/plain", body="fixture denial")
                        else:
                            route.abort()

                    self.context.route("**/*", fulfill)

            config = ProjectBrowserConfig(source, Path(temporary) / source, origin,
                resource_paths=((image_host, "/"),), max_seconds=60)
            provider = ProjectBrowserProvider(config, lambda: True, lambda _: True,
                browser_factory=FixtureBrowser, resolve_host=lambda _: ["93.184.216.34"],
                image_fetcher=lambda _: output.getvalue())
            try:
                provider.open()
                started = time.monotonic()
                listing = provider.discover(limit=10)
                elapsed = time.monotonic() - started
                assert listing["project_urls"] == [detail_url]
                assert 0.6 <= elapsed < 4.5, "render_wait_not_content_driven"
                detail_result = provider.detail(detail_url)
                assert detail_result["snapshot"]["images"] == [{"url": image_url}]
                assert provider.get_image(image_url) == output.getvalue()
                assert provider.status()["state"] == "ready"
                assert seen["optional_403"] == 1
                results.append({"source": source, "passed": True,
                    "listingRenderSeconds": round(elapsed, 3), "hiddenLoginIgnored": True,
                    "optional403Ignored": True, "imageBytes": len(output.getvalue()), **seen})
            finally:
                provider.close()
    print(json.dumps({"offlineFixtureOnly": True, "realChromium": True,
        "realSourceAcceptance": False, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
