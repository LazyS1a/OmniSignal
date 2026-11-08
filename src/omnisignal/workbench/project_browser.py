"""Experimental normal-browser OG readers. No live site acceptance is implied."""
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit

from omnisignal.knowledge.release import _lock
from omnisignal.knowledge.curation import inspect_image
from .browser_collection import (BrowserCollectionError, PinterestBrowserProvider,
    _url, _public_addresses, _safe_error)
from .source_adapters import (SOURCE_CONFIGS, SourceAdapterError, discover_projects,
    extract_project_metadata, project_identity, _document)

SOURCES = frozenset({'huaban', 'zcool', 'behance'})


@dataclass(frozen=True)
class ProjectBrowserConfig:
    source_id: str
    profile_dir: Path
    listing_url: str
    headless: bool = True
    max_scrolls: int = 0
    max_requests: int = 120
    max_seconds: int = 120
    resource_paths: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if (self.source_id not in SOURCES or not Path(self.profile_dir).is_absolute()
            or type(self.headless) is not bool or type(self.max_scrolls) is not int
            or self.max_scrolls != 0 or type(self.max_requests) is not int
            or not 1 <= self.max_requests <= 500 or type(self.max_seconds) is not int
            or not 5 <= self.max_seconds <= 300):
            raise BrowserCollectionError('invalid_config')
        spec = SOURCE_CONFIGS[self.source_id]
        _url(self.listing_url, spec['page_hosts'])
        if re.search(r'/(?:login|signin|signup|settings|account|oauth|api)(?:/|$)', urlsplit(self.listing_url).path, re.I):
            raise BrowserCollectionError('invalid_config')
        if not isinstance(self.resource_paths, tuple) or len(self.resource_paths) > 30:
            raise BrowserCollectionError('invalid_config')
        for entry in self.resource_paths:
            if (not isinstance(entry, tuple) or len(entry) != 2 or entry[0] not in spec['hosts']
                or not isinstance(entry[1], str) or not entry[1].startswith('/')
                or any(x in entry[1] for x in ('?', '#', '\\', '..', '%'))
                or (entry[0] in spec['page_hosts'] and entry[1] == '/')):
                raise BrowserCollectionError('invalid_config')


def prepare_project_profile(directory, source):
    directory = Path(directory)
    if source not in SOURCES or not directory.is_absolute() or directory.resolve() != directory.absolute() or directory.is_symlink():
        raise BrowserCollectionError('profile_unmanaged')
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / '.omnisignal-project-profile.json'
    expected = {'schema_version': 1, 'owner': 'omnisignal', 'source_id': source, 'purpose': 'project-collection'}
    if marker.exists():
        try:
            valid = not marker.is_symlink() and marker.stat().st_size <= 1024 and json.loads(marker.read_text()) == expected
        except (ValueError, OSError):
            valid = False
        if not valid:
            raise BrowserCollectionError('profile_unmanaged')
    elif any(directory.iterdir()):
        raise BrowserCollectionError('profile_unmanaged')
    else:
        with marker.open('x', encoding='utf-8') as stream:
            json.dump(expected, stream)
    chromium = directory / 'chromium'
    if chromium.is_symlink() or chromium.resolve() != chromium.absolute() or (chromium.exists() and not chromium.is_dir()):
        raise BrowserCollectionError('profile_unmanaged')
    return directory


class _ProjectBrowser:
    def __init__(self, profile, before_request, *, headless=True):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(str(profile / 'chromium'),
                headless=headless, chromium_sandbox=True, service_workers='block', accept_downloads=False)
            def route(r):
                request = r.request
                try:
                    kind = 'document' if request.is_navigation_request() else request.resource_type
                    r.continue_() if before_request(request.url, kind, request.method) else r.abort()
                except Exception:
                    r.abort()
            self.context.route('**/*', route)
            self.context.route_web_socket('**/*', lambda socket: socket.close())
            self.page = self.context.new_page()
            for page in self.context.pages:
                if page is not self.page:
                    page.close()
        except Exception:
            self.runtime.stop()
            raise

    def navigate(self, url):
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=25000)
        if response and response.status == 200:
            # Bounded wait for content OR a clear terminal UI, never challenge solving.
            try:
                self.page.wait_for_function(r"""() => {
                  if (document.querySelector('meta[property="og:image"][content]:not([content=""])')) return true;
                  if ([...document.querySelectorAll('a[href]')].some(a =>
                    new RegExp('^/(pins/[0-9]+|work/[^/]+[.]html|gallery/[0-9]+)').test(a.pathname))) return true;
                  return [...document.querySelectorAll('input[type="password"], iframe[src*="captcha"], #captcha-container, #aliyunCaptcha-window')]
                    .some(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none');
                }""", timeout=5000)
            except Exception:
                pass
        return response.status if response else 0

    def snapshot(self):
        visible = self.page.evaluate("""() => {
          const anyVisible = selector => [...document.querySelectorAll(selector)].some(e => {
            const style = getComputedStyle(e);
            return e.getClientRects().length > 0 && style.visibility !== 'hidden'
              && style.visibility !== 'collapse' && style.display !== 'none';
          });
          return {
            visible_login: anyVisible('input[type="password"]'),
            visible_challenge: anyVisible('iframe[src*="captcha"], #captcha-container, #aliyunCaptcha-window')
          };
        }""")
        return {'url': self.page.url, 'html': self.page.content(), **visible}

    def close(self):
        try:
            self.context.close()
        finally:
            self.runtime.stop()


class ProjectBrowserProvider(PinterestBrowserProvider):
    """Reuse only generic lifecycle/error timing; URL/profile/DOM logic is source-specific."""
    def __init__(self, config, reserve_request, access_policy, *, image_fetcher=None,
                 browser_factory=None, resolve_host=None, clock=time.monotonic):
        super().__init__(config, reserve_request, access_policy, image_fetcher=image_fetcher,
            browser_factory=browser_factory or _ProjectBrowser, resolve_host=resolve_host or _public_addresses, clock=clock)
        self._listing_urls = {_url(config.listing_url, SOURCE_CONFIGS[config.source_id]['page_hosts'])}

    def _validate_config(self):
        if not isinstance(self.config, ProjectBrowserConfig) or not callable(self.reserve):
            raise BrowserCollectionError('invalid_config')
        self.config.__post_init__()

    def status(self):
        return {**super().status(), 'source_id': self.config.source_id, 'image_scope': 'project_open_graph_only'}

    def _page_url(self, url):
        safe = _url(url, SOURCE_CONFIGS[self.config.source_id]['page_hosts'])
        path = urlsplit(safe).path
        if re.search(r'/(?:login|signin|signup)(?:/|$)', path, re.I):
            self._fail('login_required')
        if path == '/' or safe in self._listing_urls:
            return safe
        try:
            project_identity(self.config.source_id, safe)
            return safe
        except SourceAdapterError:
            self._fail('outside_scope')

    def _request(self, url, resource_type='document', method='GET'):
        if self._state not in {'ready', 'not_started'}:
            return False
        self._time()
        try:
            safe = _url(url, SOURCE_CONFIGS[self.config.source_id]['hosts'])
            p = urlsplit(safe)
            if method not in {'GET', 'HEAD'}:
                raise BrowserCollectionError('outside_scope')
            if resource_type == 'document':
                self._page_url(safe)
            elif not any(p.hostname == host and p.path.startswith(prefix) for host, prefix in self.config.resource_paths):
                raise BrowserCollectionError('outside_scope')
        except BrowserCollectionError as error:
            if resource_type != 'document' and error.code == 'outside_scope':
                self._blocked += 1
                return False
            self._fail(error.code)
        self._policy(safe)
        try:
            addresses = self.resolve(p.hostname)
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                self._fail('non_public_address')
        except BrowserCollectionError:
            raise
        except Exception:
            self._fail('network_error')
        self._time()
        if self._requests >= self.config.max_requests:
            self._fail('request_limit')
        try:
            if self.reserve() is False:
                self._fail('budget_exhausted')
        except BrowserCollectionError:
            raise
        except Exception as error:
            self._fail(_safe_error(error, 'budget_exhausted'))
        self._requests += 1
        return True

    def open(self):
        if self._browser is not None:
            return self.status()
        if self._state not in {'not_started', 'closed'}:
            self._fail(self._state)
        self._started = self.clock()
        self._policy(self.config.listing_url)
        self._time()
        root = prepare_project_profile(self.config.profile_dir, self.config.source_id)
        self._profile_lock = _lock(root)
        try:
            self._profile_lock.__enter__()
        except Exception:
            self._profile_lock = None
            self._fail('profile_busy')
        self._state = 'ready'
        try:
            self._browser = self.factory(root, self._request, headless=self.config.headless)
        except Exception:
            self.close()
            self._fail('browser_unavailable')
        return self.status()

    def _navigate(self, url):
        if self._state not in {'ready', 'not_started', 'closed'}:
            self._fail(self._state)
        url = self._page_url(url)
        if self._browser is None:
            self.open()
        self._time()
        try:
            status = self._browser.navigate(url)
            if self._state != 'ready':
                self._fail(self._state)
            if status in {401, 403, 429}:
                self._fail('access_denied')
            if status != 200:
                self._fail('network_error')
            data = self._browser.snapshot()
        except BrowserCollectionError:
            raise
        except Exception:
            self._fail(self._state if self._state != 'ready' else 'browser_error')
        self._time()
        if not isinstance(data, dict):
            self._fail('structure_unavailable')
        actual = self._page_url(data.get('url'))
        try:
            doc = _document(data.get('html'))
        except SourceAdapterError as error:
            self._fail('challenge' if str(error) == 'access_barrier' else 'structure_unavailable')
        def visible(nodes):
            return any(not any(a.get('hidden') is not None or a.get('aria-hidden') == 'true'
                or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', a.get('style', ''), re.I)
                for a in [node, *node.iterancestors()]) for node in nodes)
        challenge = data.get('visible_challenge')
        login = data.get('visible_login')
        if type(challenge) is not bool:
            challenge = visible(doc.xpath('//iframe[contains(@src,"captcha")]|//*[@id="captcha-container" or @id="aliyunCaptcha-window"]'))
        if type(login) is not bool:
            login = visible(doc.xpath('//input[translate(@type,"PASSWORD","password")="password"]'))
        if challenge:
            self._fail('challenge')
        if login:
            self._fail('login_required')
        return actual, data['html'], doc

    def _next_url(self, doc, current):
        links = doc.xpath('//a[contains(concat(" ",normalize-space(@rel)," ")," next ")]/@href|//link[@rel="next"]/@href')
        if not links:
            return None
        candidate = _url(urljoin(current, links[0]), SOURCE_CONFIGS[self.config.source_id]['page_hosts'])
        if urlsplit(candidate).path != urlsplit(self.config.listing_url).path or candidate == current:
            return None
        return candidate

    def discover(self, limit=100, resume_cursor=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            self._fail('invalid_config')
        url = self.config.listing_url
        remaining = None
        if resume_cursor is not None:
            if not isinstance(resume_cursor, dict) or type(resume_cursor.get('schema_version')) is not int:
                self._fail('invalid_cursor')
            version = resume_cursor['schema_version']
            if version == 2 and set(resume_cursor) == {'schema_version','url','remaining'}:
                remaining = resume_cursor['remaining']
                if not isinstance(remaining, list) or not 1 <= len(remaining) <= 199 or any(not isinstance(x, str) for x in remaining) or len(set(remaining)) != len(remaining):
                    self._fail('invalid_cursor')
                origin = _url(resume_cursor['url'], SOURCE_CONFIGS[self.config.source_id]['page_hosts'])
            elif version == 1 and set(resume_cursor) == {'schema_version','url','discovered_from'}:
                origin = _url(resume_cursor['discovered_from'], SOURCE_CONFIGS[self.config.source_id]['page_hosts'])
            else:
                self._fail('invalid_cursor')
            if urlsplit(origin).path != urlsplit(url).path:
                self._fail('invalid_cursor')
            # Re-read the observed page. Saved project URLs are never blindly trusted.
            self._listing_urls.add(origin)
            if remaining is None:
                actual, _, previous = self._navigate(origin)
                if self._next_url(previous, actual) != resume_cursor['url']:
                    self._fail('invalid_cursor')
                url = resume_cursor['url']
            else:
                url = origin
            self._listing_urls.add(url)
        actual, document, doc = self._navigate(url)
        try:
            projects = discover_projects(self.config.source_id, document, listing_url=actual, limit=200)
        except SourceAdapterError:
            self._fail('structure_unavailable')
        if len(projects) == 200:
            # The shared parser caps at 200. Detect overflow rather than silently
            # dropping the tail or advancing to another page with unvisited items.
            observed = set()
            for href in doc.xpath('//a[not(ancestor::nav) and not(ancestor::header)]/@href'):
                try:
                    observed.add(project_identity(self.config.source_id, urljoin(actual, href))['project_id'])
                except SourceAdapterError:
                    continue
                if len(observed) > 200:
                    self._fail('structure_unavailable')
        if remaining is not None:
            if any(item not in projects for item in remaining):
                self._fail('invalid_cursor')
            projects = remaining
        truncated = len(projects) > limit
        next_url = None if truncated else self._next_url(doc, actual)
        cursor = ({'schema_version':2,'url':actual,'remaining':projects[limit:]} if truncated else
            {'schema_version':1,'url':next_url,'discovered_from':actual} if next_url else None)
        return {'state':'ready','project_urls':projects[:limit],
            'resume_cursor':cursor,
            'pagination_state':'current_page_remaining' if truncated else 'observed_next_link' if next_url else 'pagination_unverified',
            'discovery_truncated':truncated,'exhausted':False if cursor else None,'next_url':next_url}

    def detail(self, url):
        try:
            identity = project_identity(self.config.source_id, url)
        except SourceAdapterError:
            self._fail('outside_scope')
        actual, document, _ = self._navigate(identity['source_url'])
        try:
            if project_identity(self.config.source_id, actual)['project_id'] != identity['project_id']:
                self._fail('identity_conflict')
            article = extract_project_metadata(self.config.source_id, document, actual)
        except SourceAdapterError as error:
            self._fail('identity_conflict' if str(error) == 'project_metadata_mismatch' else 'structure_unavailable')
        snapshot = {'project_url':identity['source_url'],'title':article['title'],
            'description':article['text'],'images':[{'url':c['url']} for c in article['candidates']]}
        self._images.update(c['url'] for c in article['candidates'])
        return {'state':'ready','snapshot':snapshot,'article':article,'identity_verified':True,
            'image_scope':'project_open_graph_only','status':self.status()}

    def get_image(self, url):
        self._time()
        if self._state != 'ready':
            self._fail(self._state)
        if url not in self._images:
            self._fail('image_not_observed')
        _url(url, SOURCE_CONFIGS[self.config.source_id]['image_hosts'])
        self._policy(url)
        if not callable(self.image_fetcher):
            self._fail('image_transport_unavailable')
        try:
            data = self.image_fetcher(url)  # Existing public-DNS, budgeted image downloader.
            if not isinstance(data, bytes) or not 0 < len(data) <= 10*1024*1024:
                self._fail('image_unavailable')
            if inspect_image(data, set())['format'] not in {'JPEG','PNG','WEBP'}:
                self._fail('image_unavailable')
        except BrowserCollectionError:
            raise
        except Exception as error:
            self._fail(_safe_error(error, 'image_unavailable'))
        return data
