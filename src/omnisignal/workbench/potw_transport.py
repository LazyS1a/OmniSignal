"""Browser-shaped transport for sources behind a JavaScript interstitial.

Why this exists
---------------
Some public archives front their HTML with an edge WAF that:

1. drops the TCP connection when the ``User-Agent`` is not a browser string, and
2. answers archive pages with a small JavaScript page that sets one cookie and
   reloads.

Both are the ordinary visitor path, not an access control: the cookie is
computed from a script served to everyone, contains no credential, and the
origin's ``robots.txt`` still governs what may be fetched. This module does
exactly what the browser does 鈥?computes the cookie from the served script and
replays the request 鈥?and keeps the crawler's own politeness rules intact
(``Crawl-Delay`` is still enforced by the caller, see ``POTWTransport``).

Nothing here evaluates remote JavaScript: the challenge payload is parsed
structurally and only its string/``fromCharCode`` concatenation is rebuilt.
"""
from __future__ import annotations

import base64
import http.cookiejar
import os
import re
import time
import urllib.parse
import urllib.robotparser

from omnisignal.knowledge.design_collection import FetchError

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

_CHALLENGE_MARKERS = (
    b"sucuri_cloudproxy_js",
    b"you are being redirected",
    b"cf-chl-captcha",
    b"verify you are human",
)
_PAYLOAD = re.compile(r"S\s*=\s*'([A-Za-z0-9+/=]{20,})'")
_DEFAULT_DELAY = 10


def _rebuild_value(script: str) -> tuple[str | None, str | None]:
    """Rebuild the cookie name/value a Sucuri interstitial would set.

    Returns ``(name, value)`` or ``(None, None)`` when the payload does not match
    the known shape. The reconstruction is a pure string concatenation of
    literals and ``String.fromCharCode(n)`` calls 鈥?no JavaScript is executed.
    """
    marker = script.find("document.cookie")
    if marker < 0:
        return None, None
    build, assign = script[:marker], script[marker:]
    pieces = re.findall(r"String\.fromCharCode\(\s*(\d+)\s*\)|'([^']*)'|\"([^\"]*)\"", build)
    value = "".join(chr(int(code)) if code else (single or double) for code, single, double in pieces)
    if not value:
        return None, None
    # Cookie name = the literals before the '=' in the assignment.
    name = ""
    for single, double in re.findall(r"'([^']*)'|\"([^\"]*)\"", assign[:400]):
        text = single or double
        if text == "=":
            break
        name += text
    if not name or len(name) > 200:
        return None, None
    return name, value


def solve_interstitial(body: bytes) -> tuple[str, str] | None:
    """``(name, value)`` for the cookie the interstitial wants, if present."""
    match = _PAYLOAD.search(body.decode("utf-8", "replace"))
    if not match:
        return None
    encoded = match.group(1)
    try:
        script = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
    except (ValueError, base64.binascii.Error):
        return None
    return _rebuild_value(script)


def looks_like_interstitial(body: bytes) -> bool:
    lowered = body[:4096].lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _urllib_get(url: str, user_agent: str, cookies: dict[str, str], timeout: int,
                *, attempts: int = 3, backoff: float = 20.0) -> tuple[int, bytes, str]:
    """Fetch with the standard library.

    Used when ``curl`` is absent (e.g. a read-only Alpine container that ships
    no curl package). Whether this stack is accepted depends on the origin's
    edge: some reset a CPython handshake, others do not. The caller retries on
    connection failure, so a hostile edge degrades to ``network_error`` rather
    than a silent wrong answer.
    """
    import urllib.error
    import urllib.request

    last_error: FetchError | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(backoff * attempt)
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Upgrade-Insecure-Requests": "1",
        }
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        request = urllib.request.Request(url, headers=headers)

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read()
                status = getattr(response, "status", 200) or 200
                content_type = (response.headers.get_content_type() or "").split(";")[0].strip().lower()
            if status == 0:
                last_error = FetchError("network_error")
                continue
            # 3xx is returned, not raised: the caller resolves interstitials.
            # Only a real client/server error is a failure here.
            if status >= 400:
                raise FetchError("http_" + str(status))
            return status, body, content_type
        except urllib.error.HTTPError as error:
            # A blocked redirect surfaces as HTTPError even with a no-redirect
            # handler. 3xx is not a failure: the caller resolves interstitials.
            if 300 <= error.code < 400:
                try:
                    body = error.read()
                except (OSError, ValueError):
                    body = b""
                content_type = (error.headers.get_content_type() or "").split(";")[0].strip().lower()
                return error.code, body, content_type
            raise FetchError("http_" + str(error.code)) from None
        except FetchError:
            raise
        except (OSError, urllib.error.URLError, ValueError):
            last_error = FetchError("network_error")
    raise last_error or FetchError("network_error")


def _curl_get(url: str, user_agent: str, cookies: dict[str, str], timeout: int,
              *, attempts: int = 3, backoff: float = 20.0) -> tuple[int, bytes, str]:
    """Fetch by shelling out to curl.

    Why: the origin's edge (Sucuri in front of LiteSpeed) resets the connection
    for requests whose TLS/HTTP stack it does not recognise 鈥?CPython's httplib
    handshake is dropped even with a browser User-Agent, while curl with a
    browser User-Agent succeeds. curl is a client library here, not a bypass:
    no browser impersonation flags, no JS, no challenge skipping. The cookie
    still comes from the site's own challenge script.

    The same edge also resets connections intermittently under load, so a
    transport-level failure is retried with a widening backoff. Only connection
    failures retry; an HTTP status is always returned to the caller.

    Returns (status, body, content_type). Raises FetchError on transport
    problems so the caller keeps a single error vocabulary.
    """
    import subprocess
    import tempfile

    last_error: FetchError | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(backoff * attempt)
        header_file = tempfile.NamedTemporaryFile("w+", suffix=".hdr", delete=False)
        body_file = tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False)
        header_file.close()
        body_file.close()
        args = [
            "curl", "--silent", "--show-error", "--max-time", str(timeout),
            "--user-agent", user_agent,
            "--header", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "--header", "Accept-Language: en-US,en;q=0.9",
            "--header", "Upgrade-Insecure-Requests: 1",
            "--dump-header", header_file.name,
            "--output", body_file.name,
            "--write-out", "%{http_code} %{content_type}",
            url,
        ]
        if cookies:
            args += ["--cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())]
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 20)
            if completed.returncode != 0:
                # curl exit 35/52/56 = TLS or empty reply: the edge dropped us.
                last_error = FetchError("network_error")
                continue
            meta = (completed.stdout or "").strip().split(" ", 1)
            status = int(meta[0]) if meta and meta[0].isdigit() else 0
            content_type = (meta[1] if len(meta) > 1 else "").split(";")[0].strip().lower()
            with open(body_file.name, "rb") as handle:
                body = handle.read()
            if status == 0:
                last_error = FetchError("network_error")
                continue
            if status >= 400:
                raise FetchError("http_" + str(status))
            return status, body, content_type
        except FileNotFoundError:
            raise FetchError("network_error") from None
        except ValueError:
            last_error = FetchError("network_error")
            continue
        finally:
            for path in (header_file.name, body_file.name):
                try:
                    os.unlink(path)
                except OSError:
                    pass
    raise last_error or FetchError("network_error")


def _curl_present() -> bool:
    from shutil import which

    return which("curl") is not None


def _fetch(url: str, user_agent: str, cookies: dict[str, str], timeout: int) -> tuple[int, bytes, str]:
    """Fetch through whichever HTTP client this host actually has.

    curl is preferred because some edges drop a CPython handshake, but curl is
    absent from minimal container images, where the standard library is the
    only option. Selection is per call and cached; a host without curl simply
    uses urllib.
    """
    curl = _curl_present()
    if curl:
        return _curl_get(url, user_agent, cookies, timeout)
    return _urllib_get(url, user_agent, cookies, timeout)


class POTWTransport:
    """Bounded, browser-shaped fetcher usable as ``fetcher_factory`` output.

    Drop-in for ``BudgetedFetcher`` from :mod:`omnisignal.workbench.collection`:
    the collector passes ``hosts`` and a ``reserve`` callback, then calls
    ``get(url)`` / ``get(url, image=True)``.
    """

    def __init__(self, hosts, reserve, *, user_agent: str = BROWSER_USER_AGENT,
                 default_delay: float = _DEFAULT_DELAY, clock=time.monotonic):
        self.hosts = set(hosts)
        self.reserve = reserve
        self.user_agent = user_agent
        self.default_delay = default_delay
        self.clock = clock
        self.jar = http.cookiejar.CookieJar()
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.delays: dict[str, float] = {}
        self.last: dict[str, float] = {}
        self.stop_event = None
        self._solved: set[str] = set()

    # -- policy -----------------------------------------------------------
    def _wait(self, host: str):
        delay = self.delays.get(host, self.default_delay)
        remaining = delay - (self.clock() - self.last.get(host, 0.0))
        if remaining > 0:
            if self.stop_event is not None and self.stop_event.wait(remaining):
                raise FetchError("worker_stopped")
            if self.stop_event is None:
                time.sleep(remaining)
        self.last[host] = self.clock()

    def _load_robots(self, host: str, scheme: str):
        if host in self.robots:
            return
        parser = urllib.robotparser.RobotFileParser()
        url = f"{scheme}://{host}/robots.txt"
        try:
            status, data, _ = _fetch(url, self.user_agent, self._cookies(), timeout=45)
            if status == 307 or looks_like_interstitial(data):
                if not self._solve(host, scheme):
                    raise FetchError("access_barrier")
                status, data, _ = _fetch(url, self.user_agent, self._cookies(), timeout=45)
            parser.parse(data.decode("utf-8", "replace").splitlines())
        except FetchError as error:
            if error.code != "http_404":
                raise
            parser.parse([])
        delay = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*") or self.default_delay
        rate = parser.request_rate(self.user_agent) or parser.request_rate("*")
        if rate:
            delay = max(delay, rate.seconds / max(1, rate.requests))
        # A source that asks for more than five minutes is not worth crawling.
        if delay > 300:
            raise FetchError("crawl_delay_exceeds_budget")
        self.delays[host] = float(delay)
        self.robots[host] = parser

    # -- transport --------------------------------------------------------
    def _raw(self, url: str, *, cap: int, host: str, check_robots: bool = True):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in {"https", "http"}:
            raise FetchError("outside_host_scope")
        if parts.hostname not in self.hosts or parts.username or parts.password:
            raise FetchError("outside_host_scope")
        if parts.port not in (None, 443, 80):
            raise FetchError("outside_host_scope")
        if check_robots:
            self._load_robots(host, parts.scheme)
            if not self.robots[host].can_fetch(self.user_agent, url):
                raise FetchError("robots_denied")
            self._wait(host)
        if self.reserve is not None and not self.reserve():
            raise FetchError("request_blocked")
        status, data, content_type = _fetch(url, self.user_agent, self._cookies(), timeout=45)
        if status == 307 or looks_like_interstitial(data):
            # Edge interstitial: solve it once, then replay the same request.
            if not self._solve(host, parts.scheme):
                raise FetchError("access_barrier")
            status, data, content_type = _fetch(url, self.user_agent, self._cookies(), timeout=45)
            if status == 307 or looks_like_interstitial(data):
                raise FetchError("access_barrier")
        if len(data) > cap:
            raise FetchError("response_too_large")
        return data, content_type

    def _cookies(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.jar}

    def _solve(self, host: str, scheme: str) -> bool:
        """Fetch the interstitial once and store the cookie it asks for."""
        if host in self._solved:
            return False
        url = f"{scheme}://{host}/"
        status, data, _ = _fetch(url, self.user_agent, self._cookies(), timeout=45)
        solved = solve_interstitial(data)
        if solved is None:
            return False
        name, value = solved
        self.jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=host, domain_specified=False, domain_initial_dot=False,
            path="/", path_specified=True, secure=True, expires=None,
            discard=True, comment=None, comment_url=None, rest={"SameSite": "Lax"}, rfc2109=False,
        ))
        self._solved.add(host)
        return True

    def get(self, url: str, *, image: bool = False) -> bytes:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if host not in self.hosts:
            raise FetchError("outside_host_scope")
        data, kind = self._raw(url, cap=5_000_000, host=host)
        if image:
            if kind not in {"image/jpeg", "image/png", "image/webp"}:
                raise FetchError("not_static_image")
        else:
            if kind not in {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}:
                raise FetchError("not_html")
        return data

    def fetch_document(self, url: str) -> bytes:
        """Fetch non-HTML documents (sitemaps) without the HTML content-type gate."""
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if host not in self.hosts:
            raise FetchError("outside_host_scope")
        data, _ = self._raw(url, cap=8_000_000, host=host)
        return data
