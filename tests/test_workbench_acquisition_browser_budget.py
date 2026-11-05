"""Synthetic scheduling and image ledgers; no browser, network or model calls."""
from datetime import datetime, timezone
import json

import pytest

from omnisignal.workbench.acquisition import (
    FAIR_CURSOR_KEY, SOURCE_QUOTA_ENV, fair_due_plans, progress, record_plan_turn,
    source_budget, source_quotas,
)
from omnisignal.workbench.store import Store
from test_workbench_acquisition import picture
from test_workbench_acquisition_review import Pages, prepared as prepare_http
from test_workbench_browser_jobs import prepared as browser_prepared, worker as browser_worker


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES', '500')
    monkeypatch.delenv(SOURCE_QUOTA_ENV, raising=False)
    return Store(tmp_path, clock=lambda: datetime(2026, 11, 5, 16, 1, tzinfo=timezone.utc).timestamp())


def add(store, source, number, at, *, actor='collector', suffix=''):
    asset=store.import_asset({'title':'synthetic', 'source_url':f'https://example.invalid/{source}/{number}/{suffix}',
                              'source_id':source}, picture(number), actor=actor)
    with store.db() as db:
        db.execute('UPDATE assets SET created_at=? WHERE id=?',(at,asset['id']))
    return asset


def plan(store, source, identity, *, enabled=True):
    value={'id':identity,'source_id':source,'name':'synthetic','interval_hours':2,'request_limit':100}
    with store.db() as db:
        db.execute('INSERT INTO plans VALUES(?,?,?,1,0,NULL)',(identity,json.dumps(value),int(enabled)))
    return {**value,'enabled':enabled,'next_run':0,'active_run':None}


def claim_turn(store, value):
    assert store.claim_plan(value['id'], force=True)
    record_plan_turn(store,value)
    with store.db() as db:
        db.execute('UPDATE plans SET active_run=NULL,next_run=0 WHERE id=?',(value['id'],))


def ready(source):
    return {'state':'ready'}


def test_explicit_source_cap_is_shared_by_plans_without_reallocation(store,monkeypatch):
    monkeypatch.setenv(SOURCE_QUOTA_ENV,json.dumps({'pinterest':300,'potw':100,'adsofbrands':100}))
    add(store,'pinterest',1,'2026-11-05T16:00:01+00:00')
    one=plan(store,'pinterest','p1');two=plan(store,'pinterest','p2')
    first=source_budget(store,one['source_id']);second=source_budget(store,two['source_id'])
    assert first==second
    assert first['source_target']==300 and first['source_remaining']==299 and first['allowed_remaining']==299
    assert source_budget(store,'zcool')['allowed_remaining']==0
    # An unavailable source does not spend or donate its reserved 100 images.
    b=plan(store,'potw','b');a=plan(store,'adsofbrands','a',enabled=False)
    assert fair_due_plans(store,[a,b,one,two],readiness=lambda s:{'state':'source_transport_unavailable' if s=='potw' else 'ready'})==[one,two]
    assert source_budget(store,'pinterest')['source_remaining']==299
    assert source_budget(store,'potw')['source_remaining']==100


def test_china_day_first_seen_content_and_manual_uploads_are_not_recounted(store):
    add(store,'potw',1,'2026-11-05T15:59:59+00:00')
    add(store,'pinterest',1,'2026-11-05T16:00:01+00:00',suffix='old-hash')
    add(store,'pinterest',2,'2026-11-05T16:00:02+00:00')
    add(store,'potw',2,'2026-11-05T16:00:03+00:00',suffix='cross-source-copy')
    add(store,'manual',3,'2026-11-05T16:00:04+00:00',actor='operator')
    add(store,'pinterest',3,'2026-11-05T16:00:05+00:00',suffix='manual-hash')
    actual=progress(store)
    assert actual['date']=='2026-11-06'
    assert actual['new_unique_images']==1 and actual['by_source']=={'pinterest':1}
    assert actual['remaining']==499


@pytest.mark.parametrize('raw',['','not-json','[]','null','{"pinterest":true}','{"pinterest":-1}',
    '{"pinterest":501}','{"pinterest":300,"potw":201}','{"pinterest":1,"pinterest":2}',
    '{"pinterest":2.5}','{"bad source":1}'])
def test_malformed_or_overallocated_policy_never_falls_back_to_unlimited(store,monkeypatch,raw):
    monkeypatch.setenv(SOURCE_QUOTA_ENV,raw)
    with pytest.raises(ValueError,match='^invalid_collection_source_quotas$'):
        source_budget(store,'pinterest')


def test_legacy_zero_global_target_keeps_explicit_caps_finite(store,monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES','0')
    assert source_budget(store,'pinterest')['allowed_remaining'] is None
    monkeypatch.setenv(SOURCE_QUOTA_ENV,'{"pinterest":300,"potw":200}')
    value=source_budget(store,'pinterest')
    assert value['global_remaining'] is None and value['source_remaining']==value['allowed_remaining']==300
    assert value['configured_source_total']==500
    assert source_budget(store,'huaban')['allowed_remaining']==0
    monkeypatch.setenv(SOURCE_QUOTA_ENV,'{"pinterest":2000,"potw":1}')
    with pytest.raises(ValueError):source_budget(store,'pinterest')


def test_absent_policy_preserves_old_global_budget_and_empty_policy_denies_all(store,monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES','100')
    original=source_budget(store,'pinterest')
    assert original['allowed_remaining']==100 and original['source_target'] is None
    monkeypatch.setenv(SOURCE_QUOTA_ENV,'{}')
    assert source_budget(store,'pinterest')['allowed_remaining']==0
    assert source_budget(store,'pinterest')['unallocated_target']==100


def test_round_robin_persists_across_restart_and_rotates_same_source_plans(store):
    a1=plan(store,'adsofbrands','a1');a2=plan(store,'adsofbrands','a2');p=plan(store,'pinterest','p');w=plan(store,'potw','w')
    values=[w,a2,p,a1]
    assert [x['id'] for x in fair_due_plans(store,values,readiness=ready)]==['a1','p','w','a2']
    # Looking at the queue does not consume a turn.
    with store.db() as db:assert db.execute('SELECT value FROM meta WHERE key=?',(FAIR_CURSOR_KEY,)).fetchone() is None
    claim_turn(store,a1)
    restarted=Store(store.root,clock=store.clock)
    assert [x['id'] for x in fair_due_plans(restarted,values,readiness=ready)]==['p','w','a2','a1']
    claim_turn(restarted,p)
    assert [x['id'] for x in fair_due_plans(restarted,values,readiness=ready)]==['w','a2','p','a1']
    # A newly due source is still visited after the persisted last source.
    p['enabled']=False
    assert [x['id'] for x in fair_due_plans(restarted,values,readiness=ready)]==['w','a2','a1']


def test_failed_claim_and_unready_plans_do_not_advance_cursor(store):
    a=plan(store,'adsofbrands','a');p=plan(store,'pinterest','p')
    with pytest.raises(ValueError,match='collection_turn_requires_claimed_plan'):record_plan_turn(store,a)
    assert fair_due_plans(store,[p,a],readiness=lambda s:{'state':'ready' if s=='adsofbrands' else 'source_not_validated'})==[a]
    with store.db() as db:assert db.execute('SELECT value FROM meta WHERE key=?',(FAIR_CURSOR_KEY,)).fetchone() is None


def test_global_cap_still_limits_a_source_and_cursor_is_not_reset_at_midnight(store,monkeypatch):
    monkeypatch.setenv('OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES','1')
    monkeypatch.setenv(SOURCE_QUOTA_ENV,'{"pinterest":1}')
    p=plan(store,'pinterest','p');claim_turn(store,p)
    add(store,'pinterest',1,'2026-11-05T16:00:01+00:00')
    assert source_budget(store,'pinterest')['allowed_remaining']==0
    assert fair_due_plans(store,[p],readiness=ready)==[]
    before=None
    with store.db() as db:before=db.execute('SELECT value FROM meta WHERE key=?',(FAIR_CURSOR_KEY,)).fetchone()[0]
    tomorrow=Store(store.root,clock=lambda:store.clock()+86400)
    assert source_budget(tomorrow,'pinterest')['allowed_remaining']==1
    with tomorrow.db() as db:assert db.execute('SELECT value FROM meta WHERE key=?',(FAIR_CURSOR_KEY,)).fetchone()[0]==before


def test_bad_persisted_cursor_fails_closed(store):
    a=plan(store,'adsofbrands','a')
    with store.db() as db:db.execute('INSERT INTO meta VALUES(?,?)',(FAIR_CURSOR_KEY,'{"last_source":false}'))
    with pytest.raises(ValueError,match='invalid_collection_fair_cursor'):fair_due_plans(store,[a],readiness=ready)


def test_http_zero_global_with_explicit_source_cap_resumes_same_project_next_day(tmp_path, monkeypatch):
    source = Pages([1], images={1: [71, 72, 73]})
    ledger, pid, worker, clock = prepare_http(tmp_path, monkeypatch, source, target=0)
    monkeypatch.setenv(SOURCE_QUOTA_ENV, '{"bpando":2}')
    assert worker.run(pid)['imported'] == 2
    assert source_budget(ledger, 'bpando')['allowed_remaining'] == 0
    assert worker.run(pid, force=True) is None
    clock[0] += 86400
    from omnisignal.workbench.collection import WorkbenchCollector
    # Restarting must preserve the partially processed project's image receipt.
    restarted = WorkbenchCollector(Store(ledger.root, clock=ledger.clock),
        fetcher_factory=source.factory, clock=ledger.clock)
    assert restarted.sweep()[0]['imported'] == 1
    assert ledger.list_assets(status='all')['total'] == 3
    assert source_budget(ledger, 'bpando')['source_used'] == 1
    assert source.calls.count('https://bpando.org/71.png') == 1
    assert source.calls.count('https://bpando.org/72.png') == 1
    assert source.calls.count('https://bpando.org/73.png') == 1


def test_http_zero_global_with_source_caps_deduplicates_content_across_projects(tmp_path, monkeypatch):
    source = Pages([1, 2], images={1: [71], 2: [71, 72]})
    ledger, pid, worker, _ = prepare_http(tmp_path, monkeypatch, source, target=0)
    monkeypatch.setenv(SOURCE_QUOTA_ENV, '{"bpando":10}')
    assert worker.run(pid)['imported'] == 2
    assert ledger.list_assets(status='all')['total'] == 2
    assert source_budget(ledger, 'bpando')['source_used'] == 2
    assert worker.run(pid, force=True)['imported'] == 0
    with ledger.db() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE actor='collector' AND action='import'").fetchone()[0] == 2


@pytest.mark.parametrize('stop_code', ['request_limit', 'time_limit', 'profile_busy'])
def test_browser_per_run_limits_continue_on_next_due_without_manual_force(browser_prepared, stop_code):
    from omnisignal.workbench.browser_jobs import BrowserJobCursor, session_state
    ledger, plan_value, clock, transport = browser_prepared
    transport.failure = stop_code
    result = browser_worker(ledger, transport).sweep()[0]
    assert result['status'] == 'stopped' and result['imported'] == 0
    cursor = BrowserJobCursor(ledger, plan_value['id'], 'pinterest', 'https://www.pinterest.com/')
    assert cursor.state['held'] is False and len(cursor.state['pending']) == 3
    assert session_state(ledger, 'pinterest').get('state') not in {'blocked', 'expired'}
    transport.failure = None
    clock[0] += 3601
    assert browser_worker(ledger, transport).sweep()[0]['imported'] == 2
    assert len([call for call in transport.calls if call[0] == 'listing']) == 1
