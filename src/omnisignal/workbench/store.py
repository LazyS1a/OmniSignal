"""Persistent editorial state. No network requests or automatic approvals."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from PIL import Image, ImageOps


class Conflict(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def require(condition, message):
    if not condition:
        raise Conflict(message)


def public_url(value):
    require(isinstance(value, str) and len(value) <= 4096, "来源链接无效")
    try:
        parts = urlsplit(value)
        require(parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password, "请填写有效的来源网页链接")
    except ValueError:
        raise Conflict("来源链接无效") from None
    return value


def sources():
    # Verification here means the bounded parser is supported, not advertising suitability.
    result = [
        {"id": "adsofbrands", "label": "Ads of Brands · 户外广告", "category": "advertising", "verified": True,
         "description": "已验证户外广告项目正文与静态图片获取。广告目的及用途权限仍需逐图审核。", "requires_credentials": False},
        {"id": "bpando", "label": "BP&O", "category": "supplemental", "verified": True,
         "description": "品牌设计与包装参考。逐图审核后才能进入广告主库。", "requires_credentials": False},
        {"id": "itsnicethat", "label": "It’s Nice That", "category": "supplemental", "verified": True,
         "description": "平面设计参考。正文或图片不完整时会跳过。", "requires_credentials": False},
        {"id": "adsoftheworld", "label": "Ads of the World", "category": "advertising", "verified": False,
         "description": "候选广告来源：自动获取尚未验收，可手工导入有来源的图片。", "requires_credentials": False},
        {"id": "manual", "label": "手工导入", "category": "manual", "verified": True,
         "description": "导入已有素材，保留来源并人工确认分类与用途。", "requires_credentials": False},
    ]
    return result


class Store:
    def __init__(self, root: Path | str, clock=time.time, *, recover_publications=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.images = self.root / "images"
        self.images.mkdir(exist_ok=True)
        self.clock = clock
        self.path = self.root / "workbench.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, document TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, document TEXT NOT NULL,
                    enabled INTEGER NOT NULL, revision INTEGER NOT NULL, next_run REAL NOT NULL,
                    active_run TEXT);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, reason TEXT,
                    imported INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS usage(plan_id TEXT NOT NULL, day TEXT NOT NULL,
                    used INTEGER NOT NULL, PRIMARY KEY(plan_id,day));
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, entity TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, actor TEXT NOT NULL,
                    role TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY, count INTEGER, since REAL);
                CREATE TABLE IF NOT EXISTS releases(version TEXT PRIMARY KEY, document TEXT NOT NULL,
                    created_at TEXT NOT NULL);
            """)
            if "credential_fingerprint" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN credential_fingerprint TEXT NOT NULL DEFAULT ''")
        if recover_publications:
            self.recover_withdrawals()

    def recover_withdrawals(self, publication_root=None):
        """Signed revocations survive a crash before the editorial DB commit."""
        from omnisignal.knowledge.release import _withdrawn, KnowledgeError
        published = Path(publication_root or os.getenv("OMNISIGNAL_KNOWLEDGE_DIR", str(self.root / "published")))
        if not (published / "withdrawals").exists():
            return
        try:
            withdrawn = _withdrawn(published)
        except (KnowledgeError, OSError, ValueError):
            raise Conflict("签名撤回目录不可读取，暂停打开工作台以避免重新显示已撤回素材") from None
        if not withdrawn:
            return
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,document FROM assets").fetchall():
                if row["id"] not in withdrawn:
                    continue
                item = json.loads(row["document"])
                if item.get("publication_status") == "withdrawn" and item["status"] == "excluded":
                    continue
                item.update(publication_status="withdrawn", status="excluded", analysis_status="not_analyzed")
                item["withdrawal_reason"] = item.get("withdrawal_reason") or "根据发布端已签名的永久撤回记录恢复本地状态"
                item["revision"] += 1
                db.execute("UPDATE assets SET document=?,status='excluded',revision=? WHERE id=?",
                           (encode(item), item["revision"], item["id"]))
                self.audit(db, item["id"], "recovery", "withdraw_recovered", {"permanent": True, "receipt_status": "unconfirmed"})

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, db, entity, actor, action, details=None):
        db.execute("INSERT INTO audit(entity,actor,action,at,details) VALUES(?,?,?,?,?)",
                   (entity, actor, action, stamp(), encode(details or {})))

    def import_asset(self, meta: dict, content: bytes, actor="operator"):
        require(0 < len(content) <= 10 * 1024 * 1024, "图片应小于 10 MiB")
        require(isinstance(meta.get("title"), str) and 0 < len(meta["title"].strip()) <= 500, "请填写不超过 500 字的标题")
        source_url = public_url(meta.get("source_url"))
        require(all(isinstance(meta.get(k, ""), str) and len(meta.get(k, "")) <= n for k, n in
                    (("brand", 200), ("description", 20000), ("project_id", 256), ("source_image_url", 4096))), "素材字段过长或无效")
        try:
            with Image.open(io.BytesIO(content)) as image:
                require(image.format in {"PNG", "JPEG", "WEBP"} and not getattr(image, "is_animated", False), "首版支持静态 PNG、JPEG 和 WebP")
                require(32 <= image.width <= 16000 and 32 <= image.height <= 16000 and image.width * image.height <= 40_000_000, "图片尺寸不在支持范围内")
                width, height, kind = image.width, image.height, image.format
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                thumb = ImageOps.exif_transpose(image).convert("RGB")
                thumb.thumbnail((640, 640))
                output = io.BytesIO()
                thumb.save(output, "JPEG", quality=82)
        except Conflict:
            raise
        except Exception:
            raise Conflict("无法读取图片，请检查文件是否完整") from None
        sha = hashlib.sha256(content).hexdigest()
        asset_id = hashlib.sha256((sha + "\n" + source_url + "\n" + meta.get("project_id", "")).encode()).hexdigest()[:32]
        extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[kind]
        image_path = self.images / (sha + "." + extension)
        thumb_path = self.images / (sha + "-thumb.jpg")
        permissions = meta.get("permissions") or {}
        require(isinstance(permissions, dict) and all(v in {"unknown", "allowed", "denied"} for v in permissions.values()), "用途权限无效")
        purposes = meta.get("purposes", [])
        require(isinstance(purposes, list) and set(purposes) <= {"brand", "conversion"}, "广告分类无效")
        # Importing cannot establish permission or approval; those require an audited review.
        asset = {"id": asset_id, "title": meta["title"].strip(), "brand": meta.get("brand", ""),
                 "source_url": source_url, "source_image_url": meta.get("source_image_url", ""),
                 "source_id": meta.get("source_id", "manual"), "project_id": meta.get("project_id", asset_id),
                 "width": width, "height": height, "sha256": sha, "image_file": image_path.name,
                 "thumbnail_file": thumb_path.name, "status": "pending", "purposes": list(dict.fromkeys(purposes)),
                 "kind": "uncertain", "reason": "待人工核对广告相关性", "description": meta.get("description", ""),
                 "ocr_text": "", "permissions": {k: "unknown" for k in ("display", "analysis", "export")},
                 "permission_note": "", "revision": 1, "created_at": stamp(),
                 "legacy": meta.get("legacy"), "analysis_status": "not_analyzed"}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT document FROM assets WHERE id=?", (asset_id,)).fetchone()
            if existing:
                return {**json.loads(existing[0]), "created": False}
            for path, data in ((image_path, content), (thumb_path, output.getvalue())):
                if not path.exists():
                    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
                    temporary.write_bytes(data)
                    temporary.replace(path)
            db.execute("INSERT INTO assets VALUES(?,?,?,?,?)", (asset_id, encode(asset), "pending", 1, asset["created_at"]))
            self.audit(db, asset_id, actor, "import", {"source_id": asset["source_id"], "legacy": asset["legacy"]})
        return {**asset, "created": True}

    def asset(self, asset_id):
        with self.db() as db:
            row = db.execute("SELECT document FROM assets WHERE id=?", (asset_id,)).fetchone()
            require(row is not None, "素材不存在")
            result = json.loads(row[0])
            result["audit"] = [{**dict(r), "details": json.loads(r["details"])} for r in db.execute(
                "SELECT actor,action,at,details FROM audit WHERE entity=? ORDER BY id DESC LIMIT 100", (asset_id,))]
            return result

    def list_assets(self, status="accepted", q="", purpose="", page=1, page_size=24):
        require(status in {"accepted", "pending", "supplemental", "excluded", "all"}, "素材状态无效")
        require(purpose in {"", "brand", "conversion"}, "广告分类无效")
        where, args = [], []
        if status != "all":
            where.append("status=?"); args.append(status)
        if q:
            where.append("(json_extract(document,'$.title') LIKE ? ESCAPE '\\' OR json_extract(document,'$.brand') LIKE ? ESCAPE '\\' OR json_extract(document,'$.ocr_text') LIKE ? ESCAPE '\\')")
            term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([term] * 3)
        if purpose:
            where.append("EXISTS(SELECT 1 FROM json_each(assets.document,'$.purposes') WHERE value=?)"); args.append(purpose)
        condition = " WHERE " + " AND ".join(where) if where else ""
        with self.db() as db:
            total = db.execute("SELECT count(*) FROM assets" + condition, args).fetchone()[0]
            rows = db.execute("SELECT document FROM assets" + condition + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (*args, page_size, (page-1)*page_size))
            return {"items": [json.loads(r[0]) for r in rows], "total": total, "page": page, "page_size": page_size}

    def review(self, asset_id, body, actor, role):
        allowed = {"expected_revision", "status", "purposes", "reason", "kind", "title", "brand", "description", "ocr_text", "permissions", "permission_note"}
        require(not set(body) - allowed, "包含不支持的修改字段")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM assets WHERE id=?", (asset_id,)).fetchone()
            require(row is not None, "素材不存在")
            item = json.loads(row[0])
            require(type(body.get("expected_revision")) is int and body["expected_revision"] == item["revision"], "素材已被修改，请刷新后重试")
            patch = {k:v for k,v in body.items() if k != "expected_revision"}
            require("permissions" not in patch or role == "admin", "用途权限需要管理员核对")
            item.update(patch)
            require(item["status"] in {"pending", "accepted", "supplemental", "excluded"}, "处理状态无效")
            require(item["kind"] in {"advertisement", "reference", "uncertain"}, "素材类型无效")
            require(isinstance(item["purposes"], list) and set(item["purposes"]) <= {"brand", "conversion"}, "广告分类无效")
            for key, limit in (("title",500),("brand",200),("reason",2000),("description",20000),("ocr_text",10000),("permission_note",4000)):
                require(isinstance(item.get(key), str) and len(item[key]) <= limit, "文字字段无效或过长")
            require(bool(item["title"].strip()) and bool(item["reason"].strip()), "请填写标题和判断依据")
            if item["status"] == "accepted":
                require(item["kind"] == "advertisement" and bool(item["purposes"]), "收录前需确认广告类型及至少一种传播目的")
            if "permissions" in patch:
                require(isinstance(item["permissions"],dict) and set(item["permissions"]) == {"display", "analysis", "export"}
                        and all(v in {"allowed", "denied", "unknown"} for v in item["permissions"].values()), "用途权限无效")
                require(bool(item["permission_note"].strip()), "修改用途权限需记录依据")
            # Removing a local approval cannot revoke already signed packages.
            # Require the explicit signed withdrawal flow before reducing access.
            if item["status"] != "accepted" or any(item["permissions"].get(k) != "allowed" for k in ("display", "export")):
                from omnisignal.knowledge.release import materialize_release, KnowledgeError
                published = Path(os.getenv("OMNISIGNAL_KNOWLEDGE_DIR", str(self.root / "published")))
                if (published / "latest.json").exists():
                    try:
                        active = materialize_release(published)
                    except (KnowledgeError, OSError):
                        raise Conflict("无法核验已发布状态，暂不能降低权限；请检查发布目录") from None
                    require(asset_id not in active, "该素材已有签名发布版本，请先执行永久撤回，再变更状态或降低展示/导出权限")
            require(item.get("publication_status") != "withdrawn" or item["status"] != "accepted",
                    "此素材已永久撤回，原素材 ID 不得重新收录发布")
            item["revision"] += 1
            if item.get("analysis_suggestion_revision") != item["revision"]:
                item["analysis_status"] = "not_analyzed"
            db.execute("UPDATE assets SET document=?,status=?,revision=? WHERE id=?", (encode(item),item["status"],item["revision"],asset_id))
            self.audit(db, asset_id, actor, "review", {"revision":item["revision"], "status":item["status"], "purposes":item["purposes"], "reason":item["reason"], "permissions":item["permissions"], "permission_note":item["permission_note"]})
            return item

    def counts(self):
        with self.db() as db:
            result = {r[0]:r[1] for r in db.execute("SELECT status,count(*) FROM assets GROUP BY status")}
        return {"assets":sum(result.values()), **{k:result.get(k,0) for k in ("pending","accepted","supplemental","excluded")}}

    def known_project_ids(self, source_id):
        with self.db() as db:
            return {r[0] for r in db.execute("SELECT DISTINCT json_extract(document,'$.project_id') FROM assets WHERE json_extract(document,'$.source_id')=?",(source_id,))}

    def create_plan(self, body, actor):
        source = next((s for s in sources() if s["id"] == body.get("source_id")), None)
        require(source is not None and source["verified"] and source["category"] != "manual", "此来源尚未通过自动采集验收，请先手工导入")
        name, hours, limit = body.get("name"), body.get("interval_hours",24), body.get("request_limit",30)
        require(isinstance(name,str) and 1 <= len(name.strip()) <= 100, "请填写计划名称")
        require(type(hours) is int and 1 <= hours <= 168, "检查间隔应为 1–168 小时")
        require(type(limit) is int and 1 <= limit <= 500, "每日请求上限应为 1–500 次")
        plan = {"id":uuid.uuid4().hex, "name":name.strip(), "source_id":source["id"], "interval_hours":hours,
                "request_limit":limit, "created_at":stamp()}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db.execute("SELECT count(*) FROM plans").fetchone()[0] < 20, "试点最多创建 20 个计划")
            # Paused by default; creating a plan never starts network work.
            db.execute("INSERT INTO plans VALUES(?,?,0,1,0,NULL)", (plan["id"],encode(plan)))
            self.audit(db,plan["id"],actor,"plan_created",plan)
        return next(x for x in self.list_plans() if x["id"] == plan["id"])

    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()

    def list_plans(self, raw=False):
        with self.db() as db:
            result = []
            for row in db.execute("SELECT * FROM plans ORDER BY id"):
                item = {**json.loads(row["document"]), "enabled":bool(row["enabled"]), "revision":row["revision"], "next_run":row["next_run"], "active_run":row["active_run"]}
                if raw:
                    result.append(item); continue
                usage = db.execute("SELECT used FROM usage WHERE plan_id=? AND day=?",(item["id"],self.day())).fetchone()
                last = db.execute("SELECT * FROM runs WHERE plan_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1",(item["id"],)).fetchone()
                used = usage[0] if usage else 0
                item["last_run"] = dict(last) if last else None
                item["budget"] = {"request_limit":item["request_limit"], "used":used, "period":"day", "timezone":"UTC", "actual_cost":None, "cost_basis":"未计入外部 API 或模型费用"}
                paused, exhausted = not item["enabled"], used >= item["request_limit"]
                item["readiness"] = {"state":"budget_exhausted" if exhausted else "ready",
                    "label":"今日请求预算已用完" if exhausted else "配置就绪",
                    "reasons":["次日 UTC 预算重置后可继续"] if exhausted else [],
                    "connection":"not_checked" if not last else "last_run_available"}
                row_counts = db.execute("SELECT status,count(*) FROM assets WHERE json_extract(document,'$.source_id')=? GROUP BY status",(item["source_id"],)).fetchall()
                counter = dict(row_counts)
                item["counts"] = {"fetched":sum(counter.values()), "accepted":counter.get("accepted",0), "scope":"source"}
                result.append(item)
            return result

    def control_plan(self, plan_id, body, actor):
        action = body.get("action")
        require(action in {"pause", "resume", "run", "check"}, "计划操作无效")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plans WHERE id=?",(plan_id,)).fetchone()
            require(row is not None, "计划不存在")
            require(type(body.get("expected_revision")) is int and body["expected_revision"] == row["revision"], "计划已被修改，请刷新后重试")
            if action == "check":
                self.audit(db,plan_id,actor,"configuration_check",{"network_request":False})
            else:
                require(action != "run" or row["enabled"], "计划已暂停，请先恢复")
                require(action != "run" or not row["active_run"], "此计划已有运行中的任务")
                enabled = 0 if action == "pause" else 1
                next_run = 0 if action == "run" else row["next_run"]
                db.execute("UPDATE plans SET enabled=?,revision=revision+1,next_run=? WHERE id=?",(enabled,next_run,plan_id))
                self.audit(db,plan_id,actor,action,{"inflight_request":"allowed_to_finish"})
        return next(x for x in self.list_plans() if x["id"] == plan_id)

    def claim_plan(self, plan_id, force=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plans WHERE id=?",(plan_id,)).fetchone()
            if not row or not row["enabled"] or row["active_run"] or (not force and row["next_run"] > self.clock()):
                return None
            plan = json.loads(row["document"])
            usage = db.execute("SELECT used FROM usage WHERE plan_id=? AND day=?",(plan_id,self.day())).fetchone()
            if usage and usage[0] >= plan["request_limit"]:
                return None
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO runs(id,plan_id,status,started_at) VALUES(?,?,'running',?)",(run_id,plan_id,stamp()))
            db.execute("UPDATE plans SET active_run=?,next_run=? WHERE id=?",(run_id,self.clock()+plan["interval_hours"]*3600,plan_id))
            self.audit(db,plan_id,"collector","run_started",{"run_id":run_id})
            return run_id

    def reserve_request(self, plan_id, run_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plans WHERE id=?",(plan_id,)).fetchone()
            if not row or not row["enabled"] or row["active_run"] != run_id:
                return False
            limit = json.loads(row["document"])["request_limit"]
            db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,0)",(plan_id,self.day()))
            result = db.execute("UPDATE usage SET used=used+1 WHERE plan_id=? AND day=? AND used<?",(plan_id,self.day(),limit))
            if not result.rowcount:
                return False
            db.execute("UPDATE runs SET requests=requests+1 WHERE id=?",(run_id,))
            return True

    def finish_run(self, plan_id, run_id, status, reason, imported):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT active_run FROM plans WHERE id=?",(plan_id,)).fetchone()
            if not row or row[0] != run_id:
                return
            db.execute("UPDATE runs SET status=?,finished_at=?,reason=?,imported=? WHERE id=?",(status,stamp(),str(reason)[:1000],imported,run_id))
            db.execute("UPDATE plans SET active_run=NULL WHERE id=?",(plan_id,))
            self.audit(db,plan_id,"collector","run_finished",{"run_id":run_id,"status":status,"imported":imported})

    def recover_runs(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE runs SET status='interrupted',finished_at=?,reason='服务重启，未自动重复未完成请求' WHERE status='running'",(stamp(),))
            db.execute("UPDATE plans SET active_run=NULL WHERE active_run IS NOT NULL")

    def heartbeat(self):
        with self.db() as db:
            db.execute("INSERT INTO meta VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(self.clock()),))

    def worker_status(self):
        with self.db() as db:
            row=db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
        return {"status":"not_started" if not row else "healthy" if self.clock()-float(row[0]) < 180 else "stale", "heartbeat":float(row[0]) if row else None}

    def migrate(self, legacy_root, actor):
        root=Path(legacy_root).resolve()
        require((root / "preparation.sqlite3").is_file(), "未找到旧素材准备区")
        created=skipped=0
        # Read-only access guarantees the old store and human revisions remain unchanged.
        connection=sqlite3.connect((root / "preparation.sqlite3").as_uri()+"?mode=ro",uri=True)
        try:
            rows=connection.execute("SELECT document,status FROM cases ORDER BY case_id").fetchall()
        finally:
            connection.close()
        failures=[]
        for raw,status in rows:
            case=json.loads(raw)
            if status == "withdrawn":
                skipped+=len(case.get("images",[])); continue
            source_map={s["source_id"]:s for s in case.get("sources",[])}
            for image in case.get("images",[]):
                try:
                    source=source_map[image["source_id"]]
                    relative=Path(image["path"])
                    path=(root/"assets"/relative).resolve()
                    require(path.is_relative_to(root/"assets") and path.is_file() and path.stat().st_size<=10*1024*1024,"旧图片路径或大小无效")
                    content=path.read_bytes()
                    require(hashlib.sha256(content).hexdigest()==image["sha256"],"旧图片哈希不一致")
                    result=self.import_asset({"title":case["title"],"brand":case.get("brand_or_team", ""),
                        "description":case.get("summary_zh", ""),"source_url":source["url"],"source_image_url":image.get("original_url", ""),
                        "project_id":case["case_id"],"source_id":case.get("ingestion",{}).get("source", "legacy"),
                        "legacy":{"case_id":case["case_id"],"revision":case["revision"],"image_id":image["image_id"],"status":status}},content,actor)
                    created+=int(result["created"]); skipped+=int(not result["created"])
                except (ValueError,KeyError,OSError):
                    failures.append({"case_id":case.get("case_id"),"image_id":image.get("image_id"),"reason":"图片或来源校验失败，旧数据保持不变"})
        with self.db() as db:
            self.audit(db,"migration",actor,"legacy_import",{"created":created,"skipped":skipped,"failed":len(failures)})
        return {"created":created,"skipped":skipped,"failures":failures,"old_store_modified":False}

    def record_release(self, result, actor):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO releases VALUES(?,?,?)",(result["version"],encode(result),stamp()))
            self.audit(db,result["version"],actor,"publish",{"receipt_status":"unconfirmed"})

    def releases(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT document FROM releases ORDER BY created_at DESC LIMIT 100")]
