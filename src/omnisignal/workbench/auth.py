"""Server-owned account configuration, revocable sessions and CSRF protection."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets

from fastapi import HTTPException, Request

COOKIE = "omni_workbench_session"
ITERATIONS = 310_000


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify(password, encoded):
    try:
        kind, rounds, salt, expected = encoded.split("$")
        if kind != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 1_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError,TypeError,AttributeError):
        return False


def accounts(store):
    path=Path(os.environ.get("OMNISIGNAL_WORKBENCH_USERS_FILE", str(store.root / "users.json")))
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > 64000:
            return []
        rows=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows,list):
            return []
        return [row for row in rows if isinstance(row,dict) and isinstance(row.get("role"),str) and row.get("role") in {"viewer","operator","admin"}
                and isinstance(row.get("username"),str) and isinstance(row.get("password_hash"),str)]
    except (ValueError,OSError,RecursionError):
        return []


def session(request: Request):
    raw=request.cookies.get(COOKIE, "")
    if not raw or len(raw)>200:
        return None
    store=request.app.state.store
    hashed=hashlib.sha256(raw.encode()).hexdigest()
    with store.db() as db:
        row=db.execute("SELECT * FROM sessions WHERE id=? AND expires>?",(hashed,store.clock())).fetchone()
    if not row:
        return None
    # Account removal, role changes and password rotation revoke existing sessions.
    account=next((a for a in accounts(store) if a["username"]==row["actor"]),None)
    if not account or account["role"] != row["role"]:
        return None
    fingerprint=hashlib.sha256(account["password_hash"].encode()).hexdigest()
    if not hmac.compare_digest(row["credential_fingerprint"],fingerprint):
        return None
    return dict(row)


def same_origin(request):
    origin=request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403,"请求来源不匹配，请从工作台页面操作")


def principal(request: Request):
    identity=session(request)
    if not identity:
        raise HTTPException(401,"请先登录工作台")
    if request.method not in {"GET","HEAD","OPTIONS"}:
        same_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""),identity["csrf"]):
            raise HTTPException(403,"会话校验失败，请刷新页面后重试")
    return identity


def operator(request: Request):
    identity=principal(request)
    if identity["role"] not in {"operator","admin"}:
        raise HTTPException(403,"当前为只读账号，审核与采集操作需要操作员权限")
    return identity


def admin(request: Request):
    identity=principal(request)
    if identity["role"] != "admin":
        raise HTTPException(403,"此操作需要管理员权限")
    return identity


def login(store, username, password, client):
    if not accounts(store):
        raise HTTPException(503,"管理员尚未配置工作台账号，请先运行初始化命令")
    key=hashlib.sha256(client.encode()).hexdigest()
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM login_attempts WHERE key=?",(key,)).fetchone()
        if row and store.clock()-row["since"] < 900 and row["count"] >= 8:
            raise HTTPException(429,"登录尝试过多，请在 15 分钟后重试")
        count=row["count"]+1 if row and store.clock()-row["since"]<900 else 1
        since=row["since"] if row and store.clock()-row["since"]<900 else store.clock()
        db.execute("INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET count=excluded.count,since=excluded.since",(key,count,since))
    user=next((a for a in accounts(store) if a["username"]==username),None)
    encoded=user["password_hash"] if user else password_hash("invalid-account", "00"*16)
    if not verify(password,encoded) or not user:
        raise HTTPException(401,"账号或密码不正确")
    raw,csrf=secrets.token_urlsafe(40),secrets.token_urlsafe(32)
    with store.db() as db:
        db.execute("DELETE FROM login_attempts WHERE key=?",(key,))
        db.execute("DELETE FROM sessions WHERE expires<=?",(store.clock(),))
        db.execute("INSERT INTO sessions(id,actor,role,csrf,expires,credential_fingerprint) VALUES(?,?,?,?,?,?)",
                   (hashlib.sha256(raw.encode()).hexdigest(),username,user["role"],csrf,store.clock()+8*3600,hashlib.sha256(user["password_hash"].encode()).hexdigest()))
    return raw,{"authenticated":True,"user":{"actor":username,"role":user["role"]},"csrf_token":csrf,"setup_required":False}
