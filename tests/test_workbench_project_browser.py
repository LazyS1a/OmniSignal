"""Offline rendered-HTML transport checks; no live site/session acceptance."""
import io
import json

from PIL import Image
import pytest

from omnisignal.workbench.project_browser import (
    ProjectBrowserConfig, ProjectBrowserProvider, prepare_project_profile,
    BrowserCollectionError, _ProjectBrowser,
)

CASES = [
    ('huaban', 'https://huaban.com/', 'https://huaban.com/pins/123/', 'https://hbimg.huabanimg.com/observed.jpg'),
    ('zcool', 'https://www.zcool.com.cn/', 'https://www.zcool.com.cn/work/ABC123.html', 'https://img.zcool.cn/observed.jpg'),
    ('behance', 'https://www.behance.net/', 'https://www.behance.net/gallery/123/Design', 'https://mir-s3-cdn-cf.behance.net/observed.jpg'),
]


class Browser:
    def __init__(self, guard, pages):
        self.guard, self.pages, self.visits = guard, pages, []
        self.closed = False

    def navigate(self, url):
        assert self.guard(url, 'document', 'GET')
        self.url = url
        self.visits.append(url)
        return self.pages[url].get('status', 200)

    def snapshot(self):
        return {'url': self.url, **self.pages[self.url]}

    def close(self):
        self.closed = True


def make(tmp_path, case=CASES[0], pages=None, **kwargs):
    source, listing, project, image = case
    default = {
        listing: {'html': f'<main><a href="{project}">Design</a></main>'},
        project: {'html': f'<meta property="og:url" content="{project}"><meta property="og:title" content="Observed project"><meta property="og:image" content="{image}"><img src="https://irrelevant.example/recommendation.jpg">'},
    }
    browsers, charged = [], []
    def factory(profile, guard, *, headless):
        browser = Browser(guard, default if pages is None else pages)
        browsers.append(browser)
        return browser
    config = kwargs.pop('config', ProjectBrowserConfig(source, tmp_path/source, listing))
    provider = ProjectBrowserProvider(config, kwargs.pop('reserve_request', lambda: charged.append(1) or True),
        kwargs.pop('access_policy', lambda url: True), browser_factory=factory,
        resolve_host=kwargs.pop('resolve_host', lambda host: ['93.184.216.34']), **kwargs)
    return provider, browsers, charged


def fails(code, call):
    with pytest.raises(BrowserCollectionError) as error:
        call()
    assert error.value.code == code


@pytest.mark.parametrize('case', CASES)
def test_actual_project_og_only_and_original_bytes(tmp_path, case):
    stream = io.BytesIO()
    Image.new('RGB', (600, 700), 'green').save(stream, 'JPEG')
    fetched = []
    provider, browsers, charged = make(tmp_path, case, image_fetcher=lambda url: fetched.append(url) or stream.getvalue())
    try:
        listing = provider.discover()
        assert listing['project_urls'] == [case[2]]
        assert listing['exhausted'] is None and listing['resume_cursor'] is None
        result = provider.detail(case[2])
        assert result['snapshot']['images'] == [{'url':case[3]}]
        assert result['image_scope'] == 'project_open_graph_only'
        assert result['identity_verified'] is True
        assert provider.get_image(case[3]) == stream.getvalue()
        assert fetched == [case[3]] and len(charged) == 2  # downloader owns image budget
        assert provider.status()['operationally_verified'] is False
    finally:
        provider.close()
    assert browsers[0].closed


def test_observed_next_cursor_revalidated_after_restart(tmp_path):
    _, home, project, _ = CASES[0]
    following = home + '?page=2'
    pages = {home:{'html':f'<a href="{project}">one</a><link rel="next" href="{following}">'},
        following:{'html':f'<a href="{project}">two</a>'}}
    first, _, _ = make(tmp_path, pages=pages)
    cursor = first.discover()['resume_cursor']
    first.close()
    second, browsers, _ = make(tmp_path, pages=pages)
    try:
        assert second.discover(resume_cursor=cursor)['project_urls'] == [project]
        assert browsers[0].visits == [home, following]
    finally:
        second.close()


def test_invented_cursor_never_navigated(tmp_path):
    _, home, _, _ = CASES[0]
    provider, browsers, _ = make(tmp_path)
    try:
        fails('invalid_cursor', lambda: provider.discover(resume_cursor={'schema_version':1,'url':home+'?page=999','discovered_from':home}))
        assert browsers[0].visits == [home]
    finally:
        provider.close()


def test_truncated_listing_does_not_skip_to_next_page(tmp_path):
    _, home, project, _ = CASES[0]
    provider, _, _ = make(tmp_path, pages={home:{'html':f'<a href="{project}">one</a><a href="/pins/456/">two</a><a rel="next" href="?page=2">next</a>'}})
    try:
        result = provider.discover(limit=1)
        assert result['discovery_truncated'] and result['resume_cursor']['schema_version'] == 2
        assert result['resume_cursor']['remaining'] == ['https://huaban.com/pins/456/']
        assert result['next_url'] is None
        resumed = provider.discover(limit=1,resume_cursor=result['resume_cursor'])
        assert resumed['project_urls'] == ['https://huaban.com/pins/456/']
        assert resumed['resume_cursor']['schema_version'] == 1
    finally:
        provider.close()


@pytest.mark.parametrize('page,code', [
    ({'status':401}, 'access_denied'), ({'status':403}, 'access_denied'), ({'status':429}, 'access_denied'),
    ({'html':'<div id="aliyunCaptcha-window">Verify</div>'}, 'challenge'),
    ({'html':'<input type="password">'}, 'login_required'),
    ({'html':'<div id="app"></div>'}, 'structure_unavailable'),
    ({'html':'<meta name="aliyun_waf_aa" content="x"><meta name="aliyun_waf_bb" content="x">'}, 'challenge'),
])
def test_terminal_main_page_state_is_sticky_without_retry(tmp_path, page, code):
    provider, browsers, _ = make(tmp_path, pages={CASES[0][1]:page})
    try:
        fails(code, provider.discover)
        fails(code, lambda: provider.detail(CASES[0][2]))
        assert len(browsers[0].visits) == 1
    finally:
        provider.close()
    assert provider.status()['state'] == code


def test_hidden_login_form_does_not_override_public_content(tmp_path):
    _, home, project, _ = CASES[0]
    provider, _, _ = make(tmp_path, pages={home:{'html':f'<a href="{project}">project</a><form style="display:none"><input type="password"></form>'}})
    try:
        assert provider.discover()['project_urls'] == [project]
    finally:
        provider.close()


@pytest.mark.parametrize('policy,code', [(None,'policy_not_configured'),(lambda u:False,'policy_denied')])
def test_policy_denial_before_profile_or_browser(tmp_path, policy, code):
    provider, browsers, charged = make(tmp_path, access_policy=policy)
    fails(code, provider.open)
    assert not browsers and not charged and not (tmp_path/'huaban').exists()


@pytest.mark.parametrize('kwargs,code', [
    ({'resolve_host':lambda host:['127.0.0.1']},'non_public_address'),
    ({'reserve_request':lambda:False},'budget_exhausted'),
])
def test_dns_and_budget_guard_all_navigation(tmp_path, kwargs, code):
    provider, browsers, _ = make(tmp_path, **kwargs)
    try:
        fails(code, provider.discover)
        assert not browsers[0].visits
    finally:
        provider.close()


def test_unrelated_resources_do_not_poison_usable_main_page(tmp_path):
    provider, _, charged = make(tmp_path)
    try:
        provider.open()
        assert provider._request('https://ads.example/tracker.js','script','GET') is False
        assert provider.status()['state'] == 'ready' and not charged
        assert provider.discover()['project_urls']
        fails('outside_scope', lambda: provider._request('https://huaban.com/api/private','document','GET'))
    finally:
        provider.close()


def test_explicit_resource_scope_still_uses_budget(tmp_path):
    config = ProjectBrowserConfig('huaban',tmp_path/'huaban',CASES[0][1],resource_paths=(('huaban.com','/assets/'),))
    provider, _, charged = make(tmp_path, config=config)
    try:
        provider.open()
        assert provider._request('https://huaban.com/assets/app.js','script','GET')
        assert len(charged) == 1
        assert not provider._request('https://huaban.com/private/api','xhr','GET')
        assert not provider._request('https://huaban.com/assets/app.js','script','POST')
    finally:
        provider.close()


@pytest.mark.parametrize('paths', [(('evil.example','/'),),(('huaban.com','/'),),(('huaban.com','/%2e/'),)])
def test_resource_config_does_not_expand_domains_or_page_scope(tmp_path, paths):
    fails('invalid_config', lambda: ProjectBrowserConfig('huaban',tmp_path/'p',CASES[0][1],resource_paths=paths))


def test_profile_source_binding_and_malformed_marker(tmp_path):
    path = tmp_path/'profile'
    prepare_project_profile(path,'huaban')
    fails('profile_unmanaged',lambda:prepare_project_profile(path,'behance'))
    (path/'.omnisignal-project-profile.json').write_text('{')
    fails('profile_unmanaged',lambda:prepare_project_profile(path,'huaban'))


def test_profile_exclusive_lock(tmp_path):
    one, _, _ = make(tmp_path)
    two, _, _ = make(tmp_path)
    try:
        one.open()
        fails('profile_busy',two.open)
    finally:
        one.close()
        two.close()


def test_mismatched_canonical_and_unobserved_images_rejected(tmp_path):
    _, _, project, image = CASES[0]
    provider, _, _ = make(tmp_path, pages={project:{'html':f'<meta property="og:url" content="https://huaban.com/pins/456/"><meta property="og:title" content="Title"><meta property="og:image" content="{image}">'}})
    try:
        fails('identity_conflict',lambda:provider.detail(project))
        assert provider.status()['observed_image_count'] == 0
    finally:
        provider.close()
    provider, _, _ = make(tmp_path)
    try:
        provider.detail(project)
        fails('image_not_observed',lambda:provider.get_image(image.replace('observed','original')))
    finally:
        provider.close()


def test_transport_wait_is_bounded_and_main_status_only():
    calls = []
    class Page:
        url = CASES[0][1]
        def goto(self,url,**kw):
            calls.append(('goto',kw))
            return type('Response',(),{'status':200})()
        def wait_for_function(self,expression,**kw):
            calls.append(('wait',kw))
            assert 'og:image' in expression and 'gallery' in expression
        def content(self): return '<main>ready</main>'
        def evaluate(self, expression):
            assert 'getComputedStyle' in expression and 'getClientRects' in expression
            return {'visible_login':False,'visible_challenge':False}
    transport = _ProjectBrowser.__new__(_ProjectBrowser)
    transport.page = Page()
    assert transport.navigate(Page.url) == 200
    assert calls == [('goto',{'wait_until':'domcontentloaded','timeout':25000}),('wait',{'timeout':5000})]
    assert transport.snapshot()['html'] == '<main>ready</main>'


@pytest.mark.parametrize('visible,expected', [(False, None),(True,'login_required')])
def test_computed_browser_visibility_overrides_hidden_css_login(tmp_path,visible,expected):
    _, home, project, _ = CASES[0]
    provider, _, _ = make(tmp_path,pages={home:{'html':f'<a href="{project}">project</a><input class="hidden-modal" type="password">',
        'visible_login':visible,'visible_challenge':False}})
    try:
        if expected:
            fails(expected,provider.discover)
        else:
            assert provider.discover()['project_urls'] == [project]
    finally:
        provider.close()


def test_page_tail_survives_new_provider_and_does_not_starve(tmp_path):
    _, home, _, _ = CASES[0]
    pages = {home:{'html':''.join(f'<a href="/pins/{x}/">{x}</a>' for x in range(150))}}
    provider, _, _ = make(tmp_path,pages=pages)
    first = provider.discover(limit=100)
    provider.close()
    restarted, _, _ = make(tmp_path,pages=pages)
    try:
        last = restarted.discover(limit=100,resume_cursor=json.loads(json.dumps(first['resume_cursor'])))
        assert len(last['project_urls']) == 50
        assert not set(last['project_urls']) & set(first['project_urls'])
        assert last['resume_cursor'] is None
    finally:
        restarted.close()


def test_unobserved_page_tail_and_oversized_page_stop_explicitly(tmp_path):
    _, home, _, _ = CASES[0]
    provider, _, _ = make(tmp_path)
    try:
        fails('invalid_cursor',lambda:provider.discover(resume_cursor={'schema_version':2,'url':home,'remaining':['https://huaban.com/pins/999/']}))
    finally:
        provider.close()
    pages = {home:{'html':''.join(f'<a href="/pins/{x}/">{x}</a>' for x in range(201))}}
    provider, _, _ = make(tmp_path,pages=pages)
    try:
        fails('structure_unavailable',provider.discover)
    finally:
        provider.close()
