"""Bounded Pinterest DOM collection from a dedicated, operator-created profile.

No cookies, storage state, or user browser profiles are read/copied by this module.
The normal browser owns its own session. Rendering is NOT a fallback for a robots
denial or an HTTP challenge: the caller's access_policy must reject those states.
Fake transports exercise the same request guard; they are not live acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import unquote, urlsplit, urlunsplit

from omnisignal.knowledge.curation import inspect_image
from omnisignal.knowledge.release import _lock
from .source_adapters import normalize_project, project_identity, SourceAdapterError


SAFE_STATES = frozenset({
    "not_started", "ready", "closed", "login_required", "challenge", "access_denied",
    "policy_not_configured", "policy_denied", "budget_exhausted", "request_limit",
    "time_limit", "outside_scope", "non_public_address", "network_error",
    "browser_unavailable", "browser_error", "profile_unmanaged", "profile_busy",
    "invalid_config", "invalid_cursor", "structure_unavailable", "identity_conflict",
    "image_unavailable", "image_transport_unavailable", "image_not_observed",
    "paused", "worker_stopped", "request_blocked", "robots_denied",
    "crawl_delay_exceeds_budget", "source_quota_reached", "http_401", "http_403", "http_429",
})


class BrowserCollectionError(ValueError):
    def __init__(self, code):
        self.code = code if code in SAFE_STATES else "browser_error"
        super().__init__(self.code)


@dataclass(frozen=True)
class BrowserCollectionConfig:
    profile_dir: Path
    listing_url: str = "https://www.pinterest.com/"
    headless: bool = True
    max_scrolls: int = 3
    max_requests: int = 120
    max_seconds: int = 120
    # Add only paths observed in an operator-approved normal session. This is
    # not a list of endpoints to call: requests still originate in the browser.
    resource_paths: tuple[tuple[str, str], ...] = (
        ("s.pinimg.com", "/webapp/"), ("i.pinimg.com", "/"),
    )

    def __post_init__(self):
        _validate_config(self)


PAGE_HOSTS = frozenset({"www.pinterest.com", "pinterest.com"})
RESOURCE_HOSTS = PAGE_HOSTS | {"s.pinimg.com", "i.pinimg.com"}
PROFILE_MARKER = ".omnisignal-pinterest-profile.json"
PROFILE_IDENTITY = {"schema_version": 1, "owner": "omnisignal", "purpose": "pinterest-collection"}


def _url(value, hosts):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or any(ord(c) < 32 for c in value):
        raise BrowserCollectionError("outside_scope")
    try:
        p = urlsplit(value)
        if p.scheme != "https" or p.hostname not in hosts or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        if any(part in {".", ".."} for part in unquote(p.path).split("/")) or "\\" in unquote(p.path):
            raise ValueError()
    except ValueError:
        raise BrowserCollectionError("outside_scope") from None
    return urlunsplit(("https", p.hostname, p.path or "/", p.query, ""))


def _page_url(value, *, listing=False):
    value = _url(value, PAGE_HOSTS)
    p = urlsplit(value)
    pin = bool(re.fullmatch(r"/pin/[0-9]+/?", p.path))
    # Public home, search and explicit user/board pages only. No resource/API,
    # settings, private-board UI, login actions or arbitrary nested paths.
    board = bool(re.fullmatch(r"/[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9_-]{1,100}/?", p.path))
    board = board and p.path.split("/")[1] not in {
        "pin", "resource", "api", "settings", "login", "logout", "oauth", "graphql", "search", "_", "business"}
    if not (p.path == "/" or p.path.rstrip("/") == "/search/pins" or board or (pin and not listing)):
        raise BrowserCollectionError("outside_scope")
    return value


def _public_addresses(host):
    return [item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def _validate_config(c):
    if (not isinstance(c, BrowserCollectionConfig) or type(c.headless) is not bool
            or type(c.max_scrolls) is not int or not 0 <= c.max_scrolls <= 10
            or type(c.max_requests) is not int or not 1 <= c.max_requests <= 500
            or type(c.max_seconds) is not int or not 5 <= c.max_seconds <= 300):
        raise BrowserCollectionError("invalid_config")
    try:
        if not Path(c.profile_dir).is_absolute():
            raise ValueError()
        _page_url(c.listing_url, listing=True)
    except (TypeError, ValueError):
        raise BrowserCollectionError("invalid_config") from None
    if not isinstance(c.resource_paths, tuple) or len(c.resource_paths) > 30:
        raise BrowserCollectionError("invalid_config")
    for entry in c.resource_paths:
        if (not isinstance(entry, tuple) or len(entry) != 2 or entry[0] not in RESOURCE_HOSTS
                or not isinstance(entry[1], str) or not entry[1].startswith("/")
                or any(token in entry[1] for token in ("?", "#", "\\", "..", "%"))
                or (entry[0] in PAGE_HOSTS and entry[1] == "/")):
            raise BrowserCollectionError("invalid_config")


def _safe_error(error, default):
    code = getattr(error, "code", None) or str(error)
    return code if code in SAFE_STATES else default


def prepare_profile(directory):
    """Claim only an empty directory, or reopen this module's own marked profile."""
    directory = Path(directory)
    if not directory.is_absolute() or directory.is_symlink():
        raise BrowserCollectionError("profile_unmanaged")
    # Reject symlink/junction parents as well; don't follow a profile alias into
    # a personal browser directory. resolve() must describe the configured path.
    if directory.resolve() != directory.absolute():
        raise BrowserCollectionError("profile_unmanaged")
    chromium = directory / "chromium"
    if (chromium.is_symlink() or chromium.resolve() != chromium.absolute()
            or (chromium.exists() and not chromium.is_dir())):
        raise BrowserCollectionError("profile_unmanaged")
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / PROFILE_MARKER
    if marker.exists():
        try:
            if marker.is_symlink() or marker.stat().st_size > 512 or json.loads(marker.read_text(encoding="utf-8")) != PROFILE_IDENTITY:
                raise ValueError()
        except (OSError, ValueError):
            raise BrowserCollectionError("profile_unmanaged") from None
    else:
        if any(directory.iterdir()):
            raise BrowserCollectionError("profile_unmanaged")
        try:
            with marker.open("x", encoding="utf-8") as stream:
                json.dump(PROFILE_IDENTITY, stream)
        except FileExistsError:
            raise BrowserCollectionError("profile_busy") from None
    return directory


# Data is read only from the rendered DOM and two already-observed JSON script
# IDs. In particular, an empty Redux pins/feeds store is not treated as a feed,
# and IDs are never turned into guessed detail/image URLs.
DOM_SNAPSHOT = r"""() => {
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const absolute = value => { try { return new URL(value, location.href).href; } catch { return ''; } };
  const meta = property => document.querySelector(`meta[property="${property}"]`)?.content || '';
  const observedImage = img => {
    const current = img.currentSrc || img.src || '';
    const choices = [{url:absolute(current),width:img.naturalWidth||null,height:img.naturalHeight||null}];
    for (const entry of (img.srcset || '').split(',')) {
      const parts = entry.trim().split(/\s+/);
      if (parts[0] && parts[1] && /^\d+w$/.test(parts[1])) choices.push({url:absolute(parts[0]),width:Number(parts[1].slice(0,-1)),height:null});
    }
    return {url:absolute(current),alt:(img.alt||'').slice(0,1000),width:img.naturalWidth||null,height:img.naturalHeight||null,choices};
  };
  const cards=[];
  for (const a of document.querySelectorAll('a[href*="/pin/"]')) {
    if (cards.length>=200) break;
    const img=a.querySelector('img');
    if (!visible(a)||!visible(img)||a.closest('nav,header')) continue;
    const observed=observedImage(img);
    cards.push({project_url:absolute(a.getAttribute('href')),title:(img.alt||a.innerText||'').trim().slice(0,500),image:observed});
  }
  const ogImage=meta('og:image'), detailImages=[];
  for (const img of document.querySelectorAll('main img,[role="main"] img')) {
    if (!visible(img)||detailImages.length>=20) continue;
    const observed=observedImage(img);
    // Bind to the page's own image rather than unrelated recommendation cards.
    if (observed.choices.some(item=>item.url===ogImage)) detailImages.push(observed);
  }
  const loginUrl=/^\/(?:login|signup)\/?$/.test(location.pathname);
  const password=[...document.querySelectorAll('input[type="password"]')].some(visible);
  const modalLogin=[...document.querySelectorAll('[role="dialog"]')].some(el=>visible(el)&&el.querySelector('input[type="password"]'));
  const bodyText=(document.body?.innerText||'').slice(0,20000);
  const challenge=!!document.querySelector('iframe[src*="captcha"],#captcha-container,#aliyunCaptcha-window') || /verify you are human|verification required|请完成安全验证|人机验证/i.test(bodyText);
  let jsonContainers={pins:null,feeds:null};
  const state=document.querySelector('script#__PWS_INITIAL_PROPS__[type="application/json"]');
  if (state && state.textContent.length<=2*1024*1024) { try {
    const initial=JSON.parse(state.textContent)?.initialReduxState;
    if (initial && typeof initial==='object') for (const key of ['pins','feeds']) if (initial[key]&&typeof initial[key]==='object') jsonContainers[key]=Object.keys(initial[key]).length;
  } catch {} }
  const next=document.querySelector('a[rel="next"][href]');
  return {url:location.href,title:(document.querySelector('h1')?.innerText||meta('og:title')||document.title||'').slice(0,500),description:(meta('og:description')||'').slice(0,20000),canonical_urls:[meta('og:url'),document.querySelector('link[rel="canonical"]')?.href||''].filter(Boolean),og_image:ogImage,detail_images:detailImages,cards,next_url:next?absolute(next.getAttribute('href')):null,login_required:loginUrl||password||modalLogin,challenge,json_containers:jsonContainers};
}"""


class _PlaywrightBrowser:
    """Production transport. No imported storage_state or altered user agent."""
    def __init__(self, profile_dir, before_request, *, headless=True):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(
                str(profile_dir / "chromium"), headless=headless,
                service_workers="block", accept_downloads=False,
                args=["--disable-background-networking", "--disable-component-update", "--disable-sync"],
            )
            def route_request(route):
                request = route.request
                try:
                    kind = "document" if request.is_navigation_request() else request.resource_type
                    if before_request(request.url, kind, request.method):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    route.abort()
            self.context.route("**/*", route_request)
            # WebSockets are not part of this observed read-only workflow.
            # Failing to install this guard is an unavailable transport, not a
            # reason to silently allow a second unbudgeted networking channel.
            self.context.route_web_socket("**/*", lambda socket: socket.close())
            self.access_status = None
            def observe_response(response):
                if response.status in {401, 403, 429}:
                    self.access_status = response.status
            self.context.on("response", observe_response)
            # No saved prior page is navigated or inspected. New page only.
            self.page = self.context.new_page()
            for old in self.context.pages:
                if old is not self.page:
                    old.close()
        except Exception:
            self.runtime.stop()
            raise

    def navigate(self, url):
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        self.page.wait_for_timeout(500)
        return response.status if response else 0

    def snapshot(self):
        data = self.page.evaluate(DOM_SNAPSHOT)
        data["access_status"] = self.access_status
        return data

    def scroll(self):
        self.page.evaluate("window.scrollBy(0, Math.min(window.innerHeight, 1000))")
        self.page.wait_for_timeout(750)

    def close(self):
        try:
            self.context.close()
        finally:
            self.runtime.stop()


class PinterestBrowserProvider:
    """Per-run provider; caller persists returned project queue and next cursor.

    access_policy(url): read-only robots/prior-denial gate; True only if allowed.
    reserve_request(): atomic daily budget reservation, False denies a request.
    image_fetcher(url): caller's existing budgeted, public-DNS, robots-aware image
      transport returning bytes. This delegation MUST count its own requests.
    browser_factory(profile_dir, before_request, headless=...): injectable normal
      browser transport implementing navigate/snapshot/scroll/close. Its requests
      MUST call before_request(url,resource_type,method), just like Playwright.
    """
    def __init__(self, config, reserve_request, access_policy, *, image_fetcher=None,
                 browser_factory=None, resolve_host=None, clock=time.monotonic):
        self.config, self.reserve, self.policy = config, reserve_request, access_policy
        self.image_fetcher = image_fetcher
        self.factory = browser_factory or _PlaywrightBrowser
        self.resolve = resolve_host or _public_addresses
        self.clock = clock
        self._browser = self._profile_lock = None
        self._state, self._requests, self._blocked = "not_started", 0, 0
        self._started = None
        self._images = set()
        self._validate_config()

    def _validate_config(self):
        _validate_config(self.config)
        if not callable(self.reserve):
            raise BrowserCollectionError("invalid_config")

    def _fail(self, code):
        self._state = code if code in SAFE_STATES else "browser_error"
        raise BrowserCollectionError(self._state)

    def status(self):
        return {"state": self._state, "source_id": "pinterest", "requests": self._requests,
                "blocked_subresources": self._blocked, "observed_image_count": len(self._images),
                "operationally_verified": False, "persistent_profile": True}

    def _time(self):
        if self._started is not None and self.clock() - self._started >= self.config.max_seconds:
            self._fail("time_limit")

    def _policy(self, url):
        if not callable(self.policy):
            self._fail("policy_not_configured")
        try:
            if self.policy(url) is not True:
                self._fail("policy_denied")
        except BrowserCollectionError:
            raise
        except Exception as error:
            self._fail(_safe_error(error, "policy_denied"))

    def _request(self, url, resource_type="document", method="GET"):
        """Every browser network attempt passes this before actual transport."""
        if self._state not in {"ready", "not_started"}:
            return False
        self._time()
        try:
            safe = _url(url, RESOURCE_HOSTS)
            p = urlsplit(safe)
            if method not in {"GET", "HEAD"}:
                raise BrowserCollectionError("outside_scope")
            if resource_type == "document":
                if p.path.rstrip("/") in {"/login", "/signup"}:
                    self._fail("login_required")
                _page_url(safe)
            elif method not in {"GET", "HEAD"} or not any(p.hostname == host and p.path.startswith(prefix) for host, prefix in self.config.resource_paths):
                raise BrowserCollectionError("outside_scope")
        except BrowserCollectionError as error:
            if resource_type != "document" and error.code == "outside_scope":
                self._blocked += 1
                return False
            self._fail(error.code)
        self._policy(safe)
        try:
            addresses = self.resolve(p.hostname)
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                self._fail("non_public_address")
        except BrowserCollectionError:
            raise
        except Exception:
            self._fail("network_error")
        self._time()
        if self._requests >= self.config.max_requests:
            self._fail("request_limit")
        try:
            if self.reserve() is False:
                self._fail("budget_exhausted")
        except BrowserCollectionError:
            raise
        except Exception as error:
            self._fail(_safe_error(error, "budget_exhausted"))
        self._requests += 1
        return True

    def open(self):
        if self._browser is not None:
            return self.status()
        if self._state not in {"not_started", "closed"}:
            self._fail(self._state)
        self._started = self.clock()
        self._policy(self.config.listing_url)
        self._time()
        profile = prepare_profile(self.config.profile_dir)
        self._profile_lock = _lock(profile)
        try:
            self._profile_lock.__enter__()
        except Exception:
            self._profile_lock = None
            self._fail("profile_busy")
        self._state = "ready"
        try:
            self._browser = self.factory(profile, self._request, headless=self.config.headless)
        except Exception:
            self.close()
            self._fail("browser_unavailable")
        return self.status()

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._profile_lock is not None:
                self._profile_lock.__exit__(None, None, None)
                self._profile_lock = None
            if self._state in {"ready", "not_started"}:
                self._state = "closed"

    def _navigate(self, url):
        _page_url(url)
        if self._browser is None:
            self.open()
        if self._state != "ready":
            self._fail(self._state)
        self._time()
        try:
            status = self._browser.navigate(url)
        except Exception:
            if self._state != "ready":
                self._fail(self._state)
            self._fail("browser_error")
        if self._state != "ready":
            self._fail(self._state)
        if status in {401, 403, 429}:
            self._fail("access_denied")
        if status != 200:
            self._fail("network_error")
        return self._snapshot()

    def _snapshot(self):
        self._time()
        if self._state != "ready":
            self._fail(self._state)
        try:
            data = self._browser.snapshot()
        except Exception:
            self._fail("browser_error")
        if not isinstance(data, dict):
            self._fail("structure_unavailable")
        if data.get("access_status") in {401, 403, 429}:
            self._fail("access_denied")
        if data.get("challenge") is True:
            self._fail("challenge")
        if data.get("login_required") is True:
            self._fail("login_required")
        try:
            _page_url(data.get("url"))
        except BrowserCollectionError:
            self._fail("outside_scope")
        return data

    def discover(self, limit=100, resume_cursor=None):
        if type(limit) is not int or not 1 <= limit <= 500:
            self._fail("invalid_config")
        url = self.config.listing_url
        if resume_cursor is not None:
            if (not isinstance(resume_cursor, dict) or set(resume_cursor) != {"schema_version", "url", "discovered_from"}
                    or resume_cursor["schema_version"] != 1):
                self._fail("invalid_cursor")
            try:
                url = _page_url(resume_cursor["url"], listing=True)
                _page_url(resume_cursor["discovered_from"], listing=True)
            except BrowserCollectionError:
                self._fail("invalid_cursor")
        data = self._navigate(url)
        projects, seen = [], set()
        truncated = False
        for round_number in range(self.config.max_scrolls + 1):
            cards = data.get("cards")
            if not isinstance(cards, list) or len(cards) > 200:
                self._fail("structure_unavailable")
            for card in cards:
                try:
                    identity = project_identity("pinterest", card["project_url"])
                except (SourceAdapterError, TypeError, KeyError):
                    continue
                if identity["project_id"] not in seen:
                    if len(projects) >= limit:
                        truncated = True
                        break
                    seen.add(identity["project_id"])
                    projects.append(identity["source_url"])
            if len(projects) >= limit or round_number == self.config.max_scrolls:
                break
            self._time()
            try:
                self._browser.scroll()
            except Exception:
                self._fail(self._state if self._state != "ready" else "browser_error")
            data = self._snapshot()
        if not projects:
            self._fail("structure_unavailable")
        next_url = None
        if data.get("next_url") and not truncated:
            try:
                next_url = _page_url(data["next_url"], listing=True)
                if next_url == url:
                    next_url = None
            except BrowserCollectionError:
                next_url = None
        cursor = {"schema_version": 1, "url": next_url, "discovered_from": url} if next_url else None
        return {"state": "ready", "project_urls": projects, "next_url": next_url,
                "resume_cursor": cursor, "exhausted": False if next_url else None,
                "pagination_state": "observed_next_link" if next_url else "pagination_unverified",
                "discovery_truncated": truncated,
                "snapshot_basis": "visible_dom", "status": self.status()}

    def detail(self, pin_url):
        try:
            identity = project_identity("pinterest", pin_url)
        except SourceAdapterError:
            self._fail("outside_scope")
        data = self._navigate(identity["source_url"])
        try:
            current = project_identity("pinterest", data["url"])
        except SourceAdapterError:
            self._fail("identity_conflict")
        if current["native_id"] != identity["native_id"]:
            self._fail("identity_conflict")
        canonical = data.get("canonical_urls", [])
        if not isinstance(canonical, list) or len(canonical) > 4:
            self._fail("identity_conflict")
        for value in canonical:
            try:
                p = urlsplit(value)
                match = re.fullmatch(r"/pin/([0-9]+)/?", p.path)
                known_host = p.hostname in PAGE_HOSTS or bool(re.fullmatch(r"[a-z]{2,3}\.pinterest\.com", p.hostname or ""))
                if p.scheme != "https" or not known_host or p.username or p.password or p.port not in (None, 443) or not match or match.group(1) != identity["native_id"]:
                    self._fail("identity_conflict")
            except ValueError:
                self._fail("identity_conflict")
        images = data.get("detail_images")
        if not isinstance(images, list) or len(images) > 20:
            self._fail("structure_unavailable")
        candidates = []
        for image in images:
            if not isinstance(image, dict):
                continue
            choices = image.get("choices") or [image]
            if not isinstance(choices, list) or len(choices) > 30:
                continue
            valid = []
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                try:
                    actual_url = _url(choice.get("url"), {"i.pinimg.com"})
                    if not re.search(r"\.(?:jpe?g|png|webp)$", urlsplit(actual_url).path, re.I):
                        continue
                    width = choice.get("width") if type(choice.get("width")) is int else 0
                    valid.append({"url": actual_url, "width": width or None,
                                  "height": choice.get("height"), "alt": image.get("alt", "")})
                except BrowserCollectionError:
                    continue
            if valid:
                candidates.append(max(valid, key=lambda c: c["width"] or 0))
        if not candidates:
            self._fail("image_unavailable")
        snapshot = {"project_url": identity["source_url"], "title": data.get("title"),
                    "description": data.get("description", ""), "images": candidates}
        try:
            article = normalize_project("pinterest", snapshot, basis="project-detail")
        except SourceAdapterError:
            self._fail("structure_unavailable")
        for candidate in article["candidates"]:
            self._images.add(candidate["url"])
        return {"state": "ready", "snapshot": snapshot, "article": article,
                "requested_url": identity["source_url"], "canonical_urls": canonical,
                "identity_verified": True, "status": self.status()}

    def get_image(self, url):
        self._time()
        if self._state != "ready":
            self._fail(self._state)
        if url not in self._images:
            self._fail("image_not_observed")
        _url(url, {"i.pinimg.com"})
        self._policy(url)
        if not callable(self.image_fetcher):
            self._fail("image_transport_unavailable")
        try:
            data = self.image_fetcher(url)
            if not isinstance(data, bytes) or not 0 < len(data) <= 10 * 1024 * 1024:
                self._fail("image_unavailable")
            result = inspect_image(data, set())
            if result["format"] not in {"JPEG", "PNG", "WEBP"}:
                self._fail("image_unavailable")
        except BrowserCollectionError:
            raise
        except Exception as error:
            self._fail(_safe_error(error, "image_unavailable"))
        return data
