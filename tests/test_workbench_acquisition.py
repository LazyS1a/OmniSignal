import io
import json
import time
from datetime import datetime, timezone

import pytest
from PIL import Image

from omnisignal.workbench.acquisition import ArchiveCursor, listing_links, progress
from omnisignal.workbench.collection import WorkbenchCollector
from omnisignal.workbench.store import Store


def picture(number):
    out = io.BytesIO()
    Image.new("RGB", (500, 500), (number % 256, number // 256, 71)).save(out, "PNG")
    return out.getvalue()


class Source:
    def __init__(self, count=40):
        self.calls = []
        self.count = count

    def factory(self, hosts, reserve):
        source = self
        class Fetcher:
            def get(self, url, *, image=False):
                assert reserve()
                source.calls.append(url)
                if image:
                    return picture(int(url.rsplit('/', 1)[-1].split('.')[0]))
                if url == "https://bpando.org/":
                    return ''.join(f'<a href="https://bpando.org/2026/10/30/case-{i}/">case</a>' for i in range(source.count)).encode()
                n = int(url.rstrip('/').split('-')[-1])
                text = 'A synthetic project describes the typography, composition, printed packaging and colour system. ' * 4
                imgs = ''.join(f'<img src="https://bpando.org/{n*3+i}.png" width="500" height="500">' for i in range(3))
                return f'<div class="article-content"><h1>Case {n}</h1><div class="wysiwyg--post"><p>{text}</p>{imgs}</div></div>'.encode()
        return Fetcher()


def plan(store):
    result = store.create_plan({'name': 'test', 'source_id': 'bpando', 'request_limit': 500}, 'test')
    store.control_plan(result['id'], {'action': 'resume', 'expected_revision': result['revision']}, 'test')
    return result['id']


def test_daily_target_counts_100_actual_unique_imports_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES', '100')
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT', '100')
    store, source = Store(tmp_path), Source()
    pid = plan(store)
    collector = WorkbenchCollector(store, fetcher_factory=source.factory)
    run = collector.run(pid)
    assert run['imported'] == 100
    assert run['acquisition']['new_unique_images'] == 100
    assert run['acquisition']['remaining'] == 0
    calls = len(source.calls)
    assert collector.run(pid, force=True) is None
    assert len(source.calls) == calls
    assert store.list_assets(status='all')['total'] == 100
    assert all(x['permissions']['display'] == 'unknown' for x in store.list_assets(status='all')['items'])


def test_collector_policy_deduplicates_content_across_projects(tmp_path, monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES', '100')
    store = Store(tmp_path)
    meta = {'title': 'same', 'source_url': 'https://bpando.org/a', 'source_id': 'bpando', 'project_id': 'a'}
    one = store.import_asset(meta, picture(1), actor='collector', deduplicate_images=True)
    two = store.import_asset({**meta, 'source_url': 'https://bpando.org/b', 'project_id': 'b'}, picture(1), actor='collector', deduplicate_images=True)
    assert one['created'] is True and two['created'] is False
    assert progress(store)['new_unique_images'] == 1


def test_manual_upload_is_not_counted_as_collected_and_shanghai_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES', '100')
    clock = [datetime(2026, 10, 30, 16, 0, 1, tzinfo=timezone.utc).timestamp()]
    store = Store(tmp_path, clock=lambda: clock[0])
    before = store.import_asset({'title': 'old', 'source_url': 'https://bpando.org/a'}, picture(1), actor='collector')
    after = store.import_asset({'title': 'new', 'source_url': 'https://bpando.org/b'}, picture(2), actor='collector')
    store.import_asset({'title': 'manual', 'source_url': 'https://bpando.org/c'}, picture(3))
    with store.db() as db:
        db.execute('UPDATE assets SET created_at=? WHERE id=?', ('2026-10-30T15:59:59+00:00', before['id']))
        db.execute('UPDATE assets SET created_at=? WHERE id=?', ('2026-10-30T16:00:00+00:00', after['id']))
    value = progress(store)
    assert value['date'] == '2026-10-31'
    assert value['new_unique_images'] == 1 and value['remaining'] == 99


def test_archive_cursor_retains_interrupted_page_and_advances_persistently(tmp_path):
    store = Store(tmp_path)
    page = b'<a href="/page/2/">Next</a><a href="/page/3/">3</a><a href="https://evil.invalid/page/4/">outside</a>'
    cursor = ArchiveCursor(store, 'bpando', 'https://bpando.org/')
    cursor.observe(page, 'https://bpando.org/')
    assert cursor.next() == 'https://bpando.org/page/2/'
    reloaded = ArchiveCursor(store, 'bpando', 'https://bpando.org/')
    assert reloaded.next() == cursor.next()
    reloaded.complete(reloaded.next())
    reloaded.observe(page, 'https://bpando.org/')
    assert reloaded.next() == 'https://bpando.org/page/3/'


@pytest.mark.parametrize('url', ['http://bpando.org/page/2/', 'https://other.invalid/page/2/', '/page/2/?token=foo', '/admin', 'https://x:y@bpando.org/page/2/'])
def test_discovery_never_follows_unrelated_or_credential_links(url):
    assert listing_links('bpando', f'<a href="{url}">Next</a>', 'https://bpando.org/') == []


def test_fewer_source_images_reports_gap_instead_of_recounting(tmp_path, monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES', '100')
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT', '100')
    store, source = Store(tmp_path), Source(2)
    pid = plan(store)
    collector = WorkbenchCollector(store, fetcher_factory=source.factory)
    assert collector.run(pid)['imported'] == 6
    assert collector.run(pid, force=True)['imported'] == 0
    assert progress(store)['remaining'] == 94
