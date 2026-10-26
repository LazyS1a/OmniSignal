import base64
import hashlib
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from omnisignal.workbench.app import create_app
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.store import Store, Conflict
from omnisignal.workbench.__main__ import backup, restore


BASE='/workbench/api'


def picture():
    target=io.BytesIO()
    Image.new('RGB',(420,560),(17,50,100)).save(target,'PNG')
    return target.getvalue()


@pytest.fixture
def app(tmp_path,monkeypatch):
    monkeypatch.delenv('OMNISIGNAL_WORKBENCH_USERS_FILE',raising=False)
    result=create_app(tmp_path/'store',start_worker=False)
    hashed=password_hash('test-only-long-password')
    (result.state.store.root/'users.json').write_text(json.dumps([
        {'username':role,'role':role,'password_hash':hashed} for role in ('viewer','operator','admin')]),encoding='utf-8')
    return result


def client(app,role='admin'):
    client=TestClient(app)
    response=client.post(BASE+'/session',json={'username':role,'password':'test-only-long-password'})
    assert response.status_code==200,response.text
    assert 'httponly' in response.headers['set-cookie'].lower()
    client.headers['X-CSRF-Token']=response.json()['csrf_token']
    return client


def imported(client):
    response=client.post(BASE+'/assets/import',json={'title':'Synthetic advertising review fixture','brand':'Synthetic',
        'source_url':'https://example.com/case','filename':'fixture.png','image_base64':base64.b64encode(picture()).decode()})
    assert response.status_code==201,response.text
    return response.json()


def test_real_import_review_filter_persistence_and_no_fabricated_approval(app):
    web=client(app)
    item=imported(web)
    assert item['status']=='pending' and set(item['permissions'].values())=={'unknown'}
    assert web.get(BASE+'/assets').json()['total']==0
    invalid=web.post(BASE+f"/assets/{item['id']}/review",json={'expected_revision':1,'status':'accepted'})
    assert invalid.status_code==409
    patch={'expected_revision':1,'status':'accepted','kind':'advertisement','purposes':['brand','conversion'],'reason':'Synthetic text and offer layout reviewed'}
    assert web.post(BASE+f"/assets/{item['id']}/review",json=patch).status_code==200
    assert web.post(BASE+f"/assets/{item['id']}/review",json=patch).status_code==409
    assert web.get(BASE+'/assets?purpose=conversion&q=Synthetic').json()['total']==1
    assert web.get(item['thumbnail_url']).headers['content-type']=='image/jpeg'
    assert web.get(item['image_url']).content==picture()
    assert web.get(BASE+f"/assets/{item['id']}").json()['audit'][0]['action']=='review'
    assert Store(app.state.store.root).counts()['accepted']==1
    repeated=imported(web)
    assert repeated['created'] is False and repeated['revision']==2 and repeated['status']=='accepted'


def test_identity_csrf_viewer_and_logout(app):
    anonymous=TestClient(app)
    assert anonymous.get(BASE+'/assets').status_code==401
    viewer=client(app,'viewer')
    assert viewer.get(BASE+'/assets').status_code==200
    assert viewer.post(BASE+'/assets/import',json={}).status_code==403
    assert viewer.post(BASE+'/migration',json={}).status_code==403
    admin=client(app)
    no_csrf=admin.post(BASE+'/plans',headers={'X-CSRF-Token':''},json={})
    assert no_csrf.status_code==403
    assert admin.post(BASE+'/plans',headers={'Origin':'https://unrelated.example'},json={}).status_code==403
    assert admin.delete(BASE+'/session').status_code==200
    assert admin.get(BASE+'/assets').status_code==401


def test_permissions_need_admin_and_evidence(app):
    operator=client(app,'operator')
    item=imported(operator)
    patch={'expected_revision':1,'permissions':{'display':'allowed','export':'allowed','analysis':'unknown'},'permission_note':'Synthetic author test permission'}
    assert operator.post(BASE+f"/assets/{item['id']}/review",json=patch).status_code==409
    administrator=client(app)
    assert administrator.post(BASE+f"/assets/{item['id']}/review",json={**patch,'permission_note':''}).status_code==409
    assert administrator.post(BASE+f"/assets/{item['id']}/review",json=patch).status_code==200


def test_assistant_review_uses_authenticated_actor_without_permission_escalation(app):
    from omnisignal.workbench.review import DESIGN_FIELDS
    operator=client(app,'operator')
    item=imported(operator)
    content={'review_method':'assistant','source_job_id':None,'summary_zh':'助手整理的自建测试样张，不是人工逐项复核。',
        'design':{field:{'text':'合成测试说明','kind':'analysis_inference'} for field in DESIGN_FIELDS}}
    patch={'expected_revision':1,'reviewed_analysis':content}
    no_csrf=operator.post(BASE+f"/assets/{item['id']}/review",json=patch,headers={'X-CSRF-Token':''})
    assert no_csrf.status_code==403
    forged=operator.post(BASE+f"/assets/{item['id']}/review",json={**patch,'reviewed_analysis':{**content,'reviewed_by':'forged'}})
    assert forged.status_code==409
    response=operator.post(BASE+f"/assets/{item['id']}/review",json=patch)
    assert response.status_code==200,response.text
    asset=response.json()
    assert asset['reviewed_analysis']['reviewed_by']=='operator'
    assert asset['reviewed_analysis']['provenance']=='assistant_editorial_review'
    assert asset['reviewed_analysis_status']=='current' and asset['revision']==2
    assert set(asset['permissions'].values())=={'unknown'}


def test_empty_state_and_unconfigured_is_truthful(app):
    web=client(app)
    assert web.get(BASE+'/summary').json()['counts']['assets']==0
    assert web.get(BASE+'/settings').json()['model']['configured'] is False
    assert web.get(BASE+'/plans').json()['items']==[]
    assert web.post(BASE+'/plans',json={'name':'Unverified','source_id':'adsoftheworld'}).status_code==409
    plan=web.post(BASE+'/plans',json={'name':'Reference','source_id':'bpando','request_limit':12}).json()
    assert not plan['enabled'] and plan['last_run'] is None and plan['budget']['used']==0
    assert web.post(BASE+f"/plans/{plan['id']}/control",json={'action':'resume','expected_revision':1}).status_code==409
    assert web.get(BASE+'/sources?include_internal=true').json()['items']


def test_budget_is_atomic_and_pause_stops_next_request(tmp_path):
    now=[1780000000.0]
    store=Store(tmp_path,clock=lambda:now[0])
    plan=store.create_plan({'name':'Bounded','source_id':'bpando','request_limit':3},'test')
    store.control_plan(plan['id'],{'action':'resume','expected_revision':1},'test')
    run=store.claim_plan(plan['id'])
    assert run and store.claim_plan(plan['id'],force=True) is None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:store.reserve_request(plan['id'],run),range(20)))
    assert sum(results)==3
    assert store.list_plans()[0]['budget']['used']==3
    now[0]+=86400
    assert store.reserve_request(plan['id'],run)
    store.control_plan(plan['id'],{'action':'pause','expected_revision':2},'test')
    assert not store.reserve_request(plan['id'],run)
    store.recover_runs()
    assert store.list_plans()[0]['last_run']['status']=='interrupted'
    assert not store.list_plans()[0]['enabled']


def test_backup_restore_preserves_review_and_revokes_sessions(app,tmp_path):
    web=client(app)
    item=imported(web)
    snapshot=backup(app.state.store.root,tmp_path/'backup')
    target=restore(snapshot,tmp_path/'restored')
    restored=Store(target)
    assert restored.asset(item['id'])['sha256']==hashlib.sha256(picture()).hexdigest()
    with restored.db() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
    (snapshot/'images'/next((snapshot/'images').iterdir()).name).write_bytes(b'broken')
    with pytest.raises(ValueError,match='完整性'):
        restore(snapshot,tmp_path/'invalid')


def test_readonly_legacy_migration_and_dedup(app,tmp_path):
    root=tmp_path/'legacy'; (root/'assets/images').mkdir(parents=True)
    data=picture(); sha=hashlib.sha256(data).hexdigest()
    (root/'assets/images/sample.png').write_bytes(data)
    case={'case_id':'old-case','revision':4,'title':'Old packaging reference','brand_or_team':'Example','summary_zh':'待整理',
          'sources':[{'source_id':'source-1','url':'https://example.com/old'}],
          'images':[{'source_id':'source-1','image_id':'image-1','path':'images/sample.png','sha256':sha}]}
    with sqlite3.connect(root/'preparation.sqlite3') as db:
        db.execute('CREATE TABLE cases(case_id TEXT,document TEXT,status TEXT)')
        db.execute('INSERT INTO cases VALUES(?,?,?)',('old-case',json.dumps(case),'approved'))
    before=(root/'preparation.sqlite3').read_bytes()
    result=app.state.store.migrate(root,'admin')
    assert result['created']==1 and not result['old_store_modified']
    assert app.state.store.migrate(root,'admin')['skipped']==1
    assert (root/'preparation.sqlite3').read_bytes()==before
    item=app.state.store.list_assets('pending')['items'][0]
    assert item['legacy']['revision']==4 and item['kind']=='uncertain'


def test_login_limits_and_account_removal_revocation(app):
    web=client(app)
    users=app.state.store.root/'users.json'
    content=json.loads(users.read_text(encoding='utf-8'))
    users.write_text(json.dumps([x for x in content if x['username']!='admin']),encoding='utf-8')
    assert web.get(BASE+'/summary').status_code==401
    other=TestClient(app)
    for _ in range(8):
        assert other.post(BASE+'/session',json={'username':'admin','password':'invalid'}).status_code==401
    assert other.post(BASE+'/session',json={'username':'admin','password':'invalid'}).status_code==429
