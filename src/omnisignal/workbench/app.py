"""Same-origin advertising UI/API. Run one process per editorial database."""
from __future__ import annotations
import base64
import binascii
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import yaml

from . import auth
from .store import Conflict, Store, encode, sources, stamp


BASE="/workbench/api"


async def body(request, maximum=32000):
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>maximum:
            raise HTTPException(413,"提交内容过大")
    try:
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key]=value
            return result
        def constant(value):
            raise ValueError("non-finite number")
        parsed=json.loads(data,object_pairs_hook=pairs,parse_constant=constant)
    except (ValueError,UnicodeError,RecursionError):
        raise HTTPException(422,"需要有效的 JSON 内容") from None
    if not isinstance(parsed,dict):
        raise HTTPException(422,"参数格式无效")
    pending=[(parsed,0)]
    while pending:
        item,depth=pending.pop()
        if depth>32:
            raise HTTPException(422,"参数嵌套层级过深")
        if isinstance(item,dict):
            pending.extend((value,depth+1) for value in item.values())
        elif isinstance(item,list):
            pending.extend((value,depth+1) for value in item)
    return parsed


def present(asset):
    result={k:v for k,v in asset.items() if k not in {"image_file","thumbnail_file","image_path"}}
    result["image_url"]=f"{BASE}/assets/{asset['id']}/image"
    result["thumbnail_url"]=f"{BASE}/assets/{asset['id']}/thumbnail"
    return result


def legacy_sources():
    path=Path(os.getenv("OMNISIGNAL_LEGACY_REGISTRY",str(Path(__file__).resolve().parents[3]/"governance/source_registry.yaml")))
    if not path.is_file():
        return []
    try:
        if path.stat().st_size>512000:
            return []
        entries=yaml.safe_load(path.read_text(encoding="utf-8")).get("sources",[])
        return [{"id":item["id"],"label":item.get("display_name",item["id"]),
                 "category":"test" if item["id"].startswith("fixture_") else "research",
                 "verified":False,"readiness":"not_checked","history":"not_connected",
                 "description":"旧控制台独立来源，未纳入广告采集计划；未连接运行历史不代表从未运行"}
                for item in entries if isinstance(item,dict) and "id" in item]
    except (OSError,ValueError,AttributeError,yaml.YAMLError):
        return []


def create_app(root=None, *, start_worker=None, frontend=None):
    store=Store(root or os.getenv("OMNISIGNAL_WORKBENCH_DIR","data/ad-workbench"))
    from .analysis import AnalysisService
    analysis=AnalysisService(store)
    enabled=start_worker if start_worker is not None else os.getenv("OMNISIGNAL_WORKBENCH_WORKER","false")=="true"

    @asynccontextmanager
    async def lifespan(app):
        worker=None
        lock=None
        if enabled:
            from omnisignal.knowledge.release import _lock
            from .collection import WorkbenchCollector
            # Holding this OS lock across the lifespan prevents a second API process
            # from recovering or executing an active worker's plans.
            lock=_lock(store.root/"worker-lock")
            lock.__enter__()
            store.recover_runs()
            worker=WorkbenchCollector(store)
            worker.start()
            analysis.start()
            app.state.collector=worker
        try:
            yield
        finally:
            if enabled:
                analysis.stop()
            if worker:
                worker.stop()
            if lock:
                lock.__exit__(None,None,None)

    app=FastAPI(title="Omni Ad Workbench",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store=store
    app.state.collector=None
    from .feed import router as feed_router
    app.include_router(feed_router(Path(os.getenv("OMNISIGNAL_KNOWLEDGE_DIR",str(store.root/"published")))))

    @app.middleware("http")
    async def headers(request, call_next):
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Frame-Options"]="DENY"
        response.headers["Content-Security-Policy"]="default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith(BASE) or request.url.path.startswith("/knowledge/v1"):
            response.headers["Cache-Control"]="no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse(status_code=409,content={"detail":str(error)})

    @app.exception_handler(sqlite3.Error)
    async def database_error(request,error):
        return JSONResponse(status_code=503,content={"detail":"工作台数据暂不可用，请稍后重试"})

    @app.exception_handler(TypeError)
    @app.exception_handler(ValueError)
    async def invalid_value(request,error):
        return JSONResponse(status_code=422,content={"detail":"参数类型或字段值无效，请核对后重试"})

    @app.get("/health/live")
    def live():
        return {"status":"alive"}

    @app.get("/health/ready")
    def ready():
        with store.db() as db:
            db.execute("SELECT count(*) FROM assets").fetchone()
        return {"status":"ready","worker_enabled":enabled}

    @app.get(BASE+"/session")
    def get_session(request:Request):
        current=auth.session(request)
        return {"authenticated":bool(current),"setup_required":not bool(auth.accounts(store)),
                "user":{"actor":current["actor"],"role":current["role"]} if current else None,
                "csrf_token":current["csrf"] if current else None}

    @app.post(BASE+"/session")
    async def login(request:Request,response:Response):
        auth.same_origin(request)
        data=await body(request,4096)
        if not isinstance(data.get("username"),str) or not isinstance(data.get("password"),str) or not 1<=len(data["username"])<=100 or not 1<=len(data["password"])<=512:
            raise HTTPException(422,"账号或密码格式无效")
        raw,result=auth.login(store,data["username"],data["password"],request.client.host if request.client else "unknown")
        response.set_cookie(auth.COOKIE,raw,max_age=8*3600,httponly=True,samesite="strict",path="/workbench",
                            secure=os.getenv("OMNISIGNAL_WORKBENCH_SECURE_COOKIE","false")=="true")
        return result

    @app.delete(BASE+"/session")
    def logout(response:Response,identity=Depends(auth.principal)):
        with store.db() as db:
            db.execute("DELETE FROM sessions WHERE id=?",(identity["id"],))
        response.delete_cookie(auth.COOKIE,path="/workbench")
        return {"authenticated":False}

    @app.get(BASE+"/summary")
    def summary(identity=Depends(auth.principal)):
        plans=store.list_plans()
        return {"counts":store.counts(),"model":analysis.configuration(),
                "sources":sources(),"budget":{"request_limit":sum(p["request_limit"] for p in plans),"used":sum(p["budget"]["used"] for p in plans),"period":"day","timezone":"UTC","actual_cost":None}}

    @app.get(BASE+"/taxonomy")
    def taxonomy(identity=Depends(auth.principal)):
        from omnisignal.knowledge.taxonomy import TAXONOMY, TAXONOMY_VERSION
        return {"version": TAXONOMY_VERSION, "groups": TAXONOMY}

    @app.get(BASE+"/assets")
    def assets(status:str="accepted",q:str=Query("",max_length=200),purpose:str="",page:int=Query(1,ge=1,le=100000),page_size:int=Query(24,ge=1,le=60),identity=Depends(auth.principal)):
        result=store.list_assets(status,q,purpose,page,page_size)
        result["items"]=[present(item) for item in result["items"]]
        return result

    @app.post(BASE+"/assets/import",status_code=201)
    async def import_asset(request:Request,identity=Depends(auth.operator)):
        data=await body(request,14*1024*1024)
        try:
            raw=data.pop("image_base64")
            if not isinstance(raw,str):
                raise ValueError()
            if raw.startswith("data:"):
                raw=raw.split(",",1)[1]
            content=base64.b64decode(raw,validate=True)
        except (KeyError,IndexError,ValueError,binascii.Error):
            raise HTTPException(422,"图片编码无效") from None
        # Do not let callers spoof collector ownership or import previous approvals.
        allowed={"title","brand","source_url","description","purposes","permissions","filename"}
        if set(data)-allowed:
            raise HTTPException(422,"导入包含不支持的字段")
        return present(store.import_asset(data,content,identity["actor"]))

    @app.get(BASE+"/assets/{asset_id}")
    def asset(asset_id:str,identity=Depends(auth.principal)):
        return present(store.asset(asset_id))

    def image_response(asset_id,thumbnail):
        item=store.asset(asset_id)
        path=store.images/item["thumbnail_file" if thumbnail else "image_file"]
        if not path.is_file():
            raise HTTPException(404,"图片暂不可用")
        if not thumbnail and hashlib.sha256(path.read_bytes()).hexdigest()!=item["sha256"]:
            raise HTTPException(503,"图片完整性检查未通过")
        return FileResponse(path,headers={"Cache-Control":"private, no-store"})

    @app.get(BASE+"/assets/{asset_id}/image")
    def image(asset_id:str,identity=Depends(auth.principal)):
        return image_response(asset_id,False)

    @app.get(BASE+"/assets/{asset_id}/thumbnail")
    def thumbnail(asset_id:str,identity=Depends(auth.principal)):
        return image_response(asset_id,True)

    @app.post(BASE+"/assets/{asset_id}/review")
    async def review(asset_id:str,request:Request,identity=Depends(auth.operator)):
        return present(store.review(asset_id,await body(request, maximum=128000),identity["actor"],identity["role"]))

    @app.get(BASE+"/plans")
    def plans(identity=Depends(auth.principal)):
        return {"items":store.list_plans(),"sources":sources(),"worker":store.worker_status()}

    @app.get(BASE+"/analysis/config")
    def analysis_config(identity=Depends(auth.principal)):
        return analysis.configuration()

    @app.post(BASE+"/analysis/batches/preview")
    async def preview_analysis_batch(request:Request,identity=Depends(auth.operator)):
        data=await body(request)
        if set(data)!={"items"}:
            raise HTTPException(422,"批量预检参数无效")
        try:
            items=analysis.batch_items(data["items"])
        except Conflict as exc:
            raise HTTPException(422,str(exc)) from None
        return analysis.preview_batch(items)

    @app.post(BASE+"/analysis/batches",status_code=202)
    async def create_analysis_batch(request:Request,identity=Depends(auth.operator)):
        data=await body(request)
        if set(data)!={"items","request_id"}:
            raise HTTPException(422,"批量分析参数无效")
        try:
            items=analysis.batch_items(data["items"])
            analysis.batch_id(data["request_id"])
        except Conflict as exc:
            raise HTTPException(422,str(exc)) from None
        if not app.state.collector:
            raise HTTPException(409,"任务执行器未启动，请联系管理员")
        return analysis.enqueue_batch(data["request_id"],items,identity["actor"])

    @app.get(BASE+"/analysis/batches/{batch_id}")
    def analysis_batch(batch_id:str,identity=Depends(auth.operator)):
        try:
            analysis.batch_id(batch_id)
        except Conflict as exc:
            raise HTTPException(422,str(exc)) from None
        result=analysis.batch(batch_id,identity["actor"])
        if result is None:
            raise HTTPException(404,"未找到此账号的分析批次")
        return result

    @app.get(BASE+"/assets/{asset_id}/analysis")
    def analysis_jobs(asset_id:str,identity=Depends(auth.principal)):
        store.asset(asset_id)
        return {"items":analysis.jobs(asset_id)}

    @app.post(BASE+"/assets/{asset_id}/analysis",status_code=202)
    async def analyze(asset_id:str,request:Request,identity=Depends(auth.operator)):
        data=await body(request)
        if set(data)-{"expected_revision","retry"} or type(data.get("expected_revision")) is not int or type(data.get("retry",False)) is not bool:
            raise HTTPException(422,"分析任务参数无效")
        if not app.state.collector:
            raise HTTPException(409,"任务执行器未启动，请联系管理员")
        return analysis.enqueue(asset_id,data["expected_revision"],identity["actor"],retry=data.get("retry",False))

    @app.post(BASE+"/plans",status_code=201)
    async def create_plan(request:Request,identity=Depends(auth.operator)):
        data=await body(request)
        if set(data)-{"name","source_id","interval_hours","request_limit"}:
            raise HTTPException(422,"计划包含不支持的字段")
        return store.create_plan(data,identity["actor"])

    @app.post(BASE+"/plans/{plan_id}/control")
    async def control_plan(plan_id:str,request:Request,identity=Depends(auth.operator)):
        data=await body(request)
        if data.get("action") in {"run","resume"} and not app.state.collector:
            raise HTTPException(409,"采集执行器未启动，请联系管理员；计划仍保持原状态")
        result=store.control_plan(plan_id,data,identity["actor"])
        if data.get("action")=="run" and app.state.collector:
            app.state.collector.enqueue(plan_id)
        return result

    @app.get(BASE+"/sources")
    def get_sources(include_internal:bool=False,identity=Depends(auth.principal)):
        if include_internal and identity["role"]!="admin":
            raise HTTPException(403,"旧来源诊断需要管理员权限")
        return {"items":sources()+(legacy_sources() if include_internal else [])}

    @app.get(BASE+"/settings")
    def settings(identity=Depends(auth.principal)):
        legacy=os.getenv("OMNISIGNAL_PREPARATION_DIR")
        return {"model":analysis.configuration(),
                "legacy":{"configured":bool(legacy),"available":bool(legacy and (Path(legacy)/"preparation.sqlite3").is_file()),"read_only":True},
                "worker":{**store.worker_status(),"enabled":bool(app.state.collector)},
                "publishing":{"configured":bool(os.getenv("OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE")),"receipt_status":"unconfirmed"},
                "budget":{"period":"day","timezone":"UTC","meaning":"每次 HTTP 请求（含失败、图片与 robots）预留一次；非账单估算"},
                "sources":legacy_sources() if identity["role"]=="admin" else []}

    @app.post(BASE+"/migration")
    def migrate(identity=Depends(auth.admin)):
        legacy=os.getenv("OMNISIGNAL_PREPARATION_DIR")
        if not legacy:
            raise HTTPException(409,"尚未配置旧素材准备区位置")
        return store.migrate(legacy,identity["actor"])

    @app.get(BASE+"/releases")
    def releases(identity=Depends(auth.principal)):
        return {"items":store.releases()}

    @app.post(BASE+"/publish")
    async def publish(request:Request,identity=Depends(auth.admin)):
        from .publishing import publish_assets
        data=await body(request)
        ids=data.get("asset_ids")
        if not isinstance(ids,list) or not 1<=len(ids)<=50 or any(not isinstance(x,str) for x in ids) or len(ids)!=len(set(ids)):
            raise HTTPException(422,"请明确选择 1–50 张素材，不可重复")
        # Keep one SQLite write transaction through preflight and signing; reviews
        # cannot revoke permissions halfway through this snapshot.
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            selected=[]
            for asset_id in ids:
                row=db.execute("SELECT document FROM assets WHERE id=?",(asset_id,)).fetchone()
                if not row:
                    raise HTTPException(404,"选中的素材不存在")
                item=json.loads(row[0]); item["image_path"]=str(store.images/item["image_file"])
                selected.append(item)
            try:
                result=publish_assets(Path(os.getenv("OMNISIGNAL_KNOWLEDGE_DIR",str(store.root/"published"))),selected,identity["actor"])
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from None
            db.execute("INSERT OR IGNORE INTO releases VALUES(?,?,?)",(result["version"],encode(result),stamp()))
            for item in selected:
                item.pop("image_path",None)
                item["publication_status"]="published"
                item["publication_version"]=result["version"]
                db.execute("UPDATE assets SET document=? WHERE id=?",(encode(item),item["id"]))
            store.audit(db,result["version"],identity["actor"],"publish",{"assets":ids,"receipt_status":"unconfirmed"})
        return result

    @app.post(BASE+"/withdraw")
    async def withdraw(request:Request,identity=Depends(auth.admin)):
        from .publishing import withdraw_assets
        data=await body(request)
        ids,reason=data.get("asset_ids"),data.get("reason")
        if (set(data)-{"asset_ids","reason"} or not isinstance(ids,list) or not 1<=len(ids)<=50
                or any(not isinstance(x,str) for x in ids) or len(ids)!=len(set(ids))
                or not isinstance(reason,str) or not 0<len(reason.strip())<=2000):
            raise HTTPException(422,"请明确选择 1–50 张已发布素材并填写永久撤回原因")
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            selected=[]
            for asset_id in ids:
                row=db.execute("SELECT document FROM assets WHERE id=?",(asset_id,)).fetchone()
                if not row:
                    raise HTTPException(404,"选中的素材不存在")
                selected.append(json.loads(row[0]))
            try:
                result=withdraw_assets(Path(os.getenv("OMNISIGNAL_KNOWLEDGE_DIR",str(store.root/"published"))),
                                       selected,identity["actor"],reason)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from None
            for item in selected:
                item["publication_status"]="withdrawn"
                item["withdrawal_version"]=result["version"]
                item["withdrawal_reason"]=reason.strip()
                item["status"]="excluded"
                item["revision"]+=1
                item["analysis_status"]="not_analyzed"
                db.execute("UPDATE assets SET document=?,status='excluded',revision=? WHERE id=?",
                           (encode(item),item["revision"],item["id"]))
                store.audit(db,item["id"],identity["actor"],"withdraw",
                            {"version":result["version"],"reason":reason.strip(),"permanent":True,"receipt_status":"unconfirmed"})
            db.execute("INSERT OR IGNORE INTO releases VALUES(?,?,?)",(result["version"],encode(result),stamp()))
        return result

    @app.get("/")
    def index():
        return RedirectResponse("/workbench/")

    static=Path(frontend or os.getenv("OMNISIGNAL_WORKBENCH_STATIC",str(Path(__file__).resolve().parents[3]/"frontend/dist")))
    if static.is_dir():
        app.mount("/workbench",StaticFiles(directory=static,html=True),name="workbench")
    else:
        @app.get("/workbench/")
        def build_required():
            return JSONResponse(status_code=503,content={"detail":"前端尚未构建，请在 frontend 执行 npm ci 和 npm run build"})
    return app
