"""Offline real scheduler/store acceptance for source-specific browser jobs."""
import io
import json
import time
from datetime import datetime, timezone

import pytest
from PIL import Image
from omnisignal.workbench.browser_jobs import browser_config, BrowserJobCursor, session_state
from omnisignal.workbench.browser_collection import BrowserCollectionError
from omnisignal.workbench.collection import WorkbenchCollector
from omnisignal.workbench.source_readiness import source_readiness
from omnisignal.workbench.source_adapters import SOURCE_CONFIGS
from omnisignal.workbench.store import Store


def project(source, number):
    return {"huaban": f"https://huaban.com/pins/{number}/", "zcool": f"https://www.zcool.com.cn/work/A{number}.html", "behance": f"https://www.behance.net/gallery/{number}/Poster"}[source]


@pytest.fixture(params=["huaban", "zcool", "behance"])
def setup(request, tmp_path, monkeypatch):
    source=request.param
    spec={"profile_dir":str(tmp_path/source), "listing_url":SOURCE_CONFIGS[source]["listing"], "resource_paths":[]}
    path=tmp_path/"browser.json"
    path.write_text(json.dumps({"version":1,"sources":{source:spec}}),encoding="utf8")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_BROWSER_CONFIG",str(path))
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS",source)
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_PUBLIC_HTML_SOURCE_IDS",raising=False)
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_DAILY_IMAGES","500")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_SOURCE_QUOTAS",json.dumps({source:2,"adsofbrands":498}))
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_PROJECT_LIMIT","100")
    now=[time.time()]
    monkeypatch.setattr("omnisignal.workbench.store.stamp",lambda:datetime.fromtimestamp(now[0],timezone.utc).isoformat())
    store=Store(tmp_path/"store",clock=lambda:now[0])
    plan=store.create_plan({"name":"Offline browser","source_id":source,"interval_hours":1,"request_limit":50},"test")
    store.control_plan(plan["id"],{"action":"resume","expected_revision":plan["revision"]},"test")
    state={"calls":[],"failure":None,"duplicate":False,"hosts":None}
    def fetcher(hosts,reserve):
        state["hosts"]=hosts
        class Fetcher:
            def check_access(self,url):return True
            def get(self,url,*,image=False):
                assert image
                reserve();state["calls"].append(("image",url))
                number=int(url.rsplit("/",1)[1].split(".")[0]); number=1 if state["duplicate"] and number==3 else number
                out=io.BytesIO();Image.new("RGB",(600,700),(number*30,70,90)).save(out,"JPEG");return out.getvalue()
        return Fetcher()
    def provider(config,reserve,access_policy,*,image_fetcher):
        assert config.source_id==source
        class Browser:
            def open(self):access_policy(config.listing_url)
            def discover(self,*,limit,resume_cursor=None):
                reserve();state["calls"].append(("listing",config.listing_url))
                return {"project_urls":[project(source,n) for n in [1,2,3]],"resume_cursor":None,"pagination_state":"pagination_unverified"}
            def detail(self,url):
                reserve();state["calls"].append(("detail",url))
                if state["failure"]:raise BrowserCollectionError(state["failure"])
                number=next(n for n in [1,2,3] if project(source,n)==url)
                return {"snapshot":{"project_url":url,"title":"Observed project","images":[{"url":"https://"+SOURCE_CONFIGS[source]["image_hosts"][0]+f"/{number}.jpg","width":600,"height":700}]}}
            def get_image(self,url):return image_fetcher(url)
            def close(self):pass
        return Browser()
    monkeypatch.setattr("omnisignal.workbench.project_browser.ProjectBrowserProvider",provider)
    worker=lambda:WorkbenchCollector(store,fetcher_factory=fetcher,clock=store.clock)
    return source,store,plan,now,state,worker,path


def test_real_job_import_quota_restart_and_no_pinterest_host(setup):
    source,store,plan,now,state,worker,_=setup
    assert source_readiness(source)["transport"]=="browser"
    assert worker().sweep()[0]["imported"]==2
    assets=store.list_assets(status="all")["items"]
    assert all(a["source_id"]==source and a["status"]=="pending" and set(a["permissions"].values())=={"unknown"} for a in assets)
    assert "s.pinimg.com" not in state["hosts"]
    assert BrowserJobCursor(store,plan["id"],source,SOURCE_CONFIGS[source]["listing"]).state["pending"]==[project(source,3)]
    now[0]+=86400
    assert worker().sweep()[0]["imported"]==1
    assert len([c for c in state["calls"] if c[0]=="listing"])==1


def test_hash_dedupe_real_store(setup,monkeypatch):
    source,store,_,_,state,worker,_=setup
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_COLLECTION_SOURCE_QUOTAS",json.dumps({source:500}))
    state["duplicate"]=True
    assert worker().sweep()[0]["imported"]==2
    assert store.list_assets(status="all")["total"]==2


@pytest.mark.parametrize("failure",["login_required","challenge"])
def test_session_failure_locks_repeated_jobs(setup,failure):
    source,store,_,now,state,worker,_=setup
    state["failure"]=failure
    assert worker().sweep()[0]["status"]=="failed"
    assert session_state(store,source)["state"] == ("expired" if failure=="login_required" else "blocked")
    calls=len(state["calls"]);now[0]+=86400
    assert worker().sweep()==[] and len(state["calls"])==calls


def test_pause_resume_preserves_cursor(setup):
    _,store,_,now,state,worker,_=setup
    worker().sweep();p=store.list_plans()[0]
    store.control_plan(p["id"],{"action":"pause","expected_revision":p["revision"]},"test")
    now[0]+=86400;assert worker().sweep()==[]
    p=store.list_plans()[0];store.control_plan(p["id"],{"action":"resume","expected_revision":p["revision"]},"test")
    assert worker().sweep()[0]["imported"]==1


def test_validation_and_no_implicit_transport(setup,monkeypatch):
    source,_,_,_,_,_,path=setup
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS")
    assert source_readiness(source)["state"]=="source_not_validated"
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_VALIDATED_SOURCE_IDS",source)
    monkeypatch.delenv("OMNISIGNAL_WORKBENCH_BROWSER_CONFIG")
    assert source_readiness(source)["state"]=="source_transport_unavailable"


def test_profiles_cannot_be_shared_or_hide_bad_scope(setup):
    source,_,_,_,_,_,path=setup
    data=json.loads(path.read_text());other=next(x for x in ["huaban","zcool","behance"] if x!=source)
    data["sources"][other]={**data["sources"][source],"listing_url":SOURCE_CONFIGS[other]["listing"]}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="browser_config_invalid"):browser_config(source)
    data["sources"].pop(other);data["sources"][source]["resource_paths"]=[["s.pinimg.com","/"]]
    path.write_text(json.dumps(data))
    assert source_readiness(source)["state"]=="browser_config_invalid"
