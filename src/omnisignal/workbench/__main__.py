"""Local setup and backup tools; secret values are written only to private files."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile

from .auth import password_hash
from .store import Store


def _copy_private_file(source, destination):
    """Copy new files with private permissions without replacing existing paths."""
    with Path(source).open("rb") as reader:
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"wb") as writer:
            shutil.copyfileobj(reader,writer,1024*1024)
    return str(destination)


def initialize(root, username, role):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",username):
        raise ValueError("账号需由英文、数字、点、横线或下划线组成")
    store=Store(root)
    path=store.root/"users.json"
    users=json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(user["username"]==username for user in users):
        raise ValueError("账号已存在；初始化不会覆盖已有账号")
    password=secrets.token_urlsafe(24)
    users.append({"username":username,"role":role,"password_hash":password_hash(password)})
    credential=store.root/("login-"+username+".txt")
    temporary=path.with_suffix(".tmp")
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        json.dump(users,f,ensure_ascii=False)
    temporary.replace(path)
    fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as f:
        f.write("Omni Ad Workbench local login\nusername: "+username+"\npassword: "+password+"\n")
    return credential


def backup(root, destination):
    store=Store(root)
    target=Path(destination).resolve()
    published=Path(os.getenv("OMNISIGNAL_KNOWLEDGE_DIR",str(store.root/"published"))).resolve()
    if target.exists() or target.is_relative_to(store.root) or target.is_relative_to(published):
        raise ValueError("备份目标必须是工作台目录之外的新目录")
    target.mkdir(parents=True,mode=0o700)
    with store.db() as source:
        with sqlite3.connect(target/"workbench.sqlite3") as dest:
            source.backup(dest)
    # Images are content-addressed and immutable. Later imports cannot change the
    # bytes referenced by the database snapshot; unused copied files are harmless.
    shutil.copytree(store.images,target/"images",copy_function=_copy_private_file)
    if (store.root/"users.json").exists():
        _copy_private_file(store.root/"users.json",target/"users.json")
    if published.is_dir():
        from omnisignal.knowledge.release import _lock
        with _lock(published):
            # Packages and cumulative revocations belong to the recoverable
            # state. A signing key must never be stored in this directory.
            for path in published.rglob("*"):
                if path.is_symlink() or (hasattr(path,"is_junction") and path.is_junction()):
                    raise ValueError("发布备份不允许链接路径")
            def ignored(directory,names):
                return [name for name in names if name.startswith(".")
                        or (Path(directory).resolve()==published and name not in {"latest.json","releases","withdrawals"})]
            shutil.copytree(published,target/"published",ignore=ignored,copy_function=_copy_private_file)
    files={str(p.relative_to(target)).replace("\\","/"):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in target.rglob("*") if p.is_file()}
    (target/"manifest.json").write_text(json.dumps({"schema":1,"files":files,"publication_directory":"published"},indent=2),encoding="utf-8")
    return target


def restore(source, destination):
    source=Path(source).resolve()
    destination=Path(destination).resolve()
    if destination.exists():
        raise ValueError("恢复目标必须是新目录；不会覆盖运行中的数据")
    manifest_path=source/"manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size>4*1024*1024:
        raise ValueError("备份清单无效")
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:
                raise ValueError("备份清单包含重复路径")
            result[key]=value
        return result
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"),object_pairs_hook=pairs)
    if (not isinstance(manifest,dict) or manifest.get("schema")!=1 or not isinstance(manifest.get("files"),dict)
            or "workbench.sqlite3" not in manifest["files"] or len(manifest["files"])>50000):
        raise ValueError("备份清单无效")
    files={}
    for name,expected in manifest["files"].items():
        if (not isinstance(name,str) or "\\" in name or ":" in name or not name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not isinstance(expected,str) or not re.fullmatch(r"[0-9a-f]{64}",expected)):
            raise ValueError("备份路径或哈希无效")
        path=source.joinpath(*name.split("/"))
        for parent in (path,*path.parents):
            if parent==source.parent:
                break
            if parent.is_symlink() or (hasattr(parent,"is_junction") and parent.is_junction()):
                raise ValueError("备份不允许链接路径")
        if not path.resolve().is_relative_to(source) or not path.is_file():
            raise ValueError("备份完整性校验失败")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream,"sha256").hexdigest()!=expected:
                raise ValueError("备份完整性校验失败")
        files[name]=expected
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=".omni-restore-",dir=destination.parent))
    try:
        for name,expected in files.items():
            path=stage.joinpath(*name.split("/"))
            if not path.resolve().is_relative_to(stage):
                raise ValueError("恢复路径越界")
            path.parent.mkdir(parents=True,exist_ok=True)
            _copy_private_file(source.joinpath(*name.split("/")),path)
            with path.open("rb") as stream:
                if hashlib.file_digest(stream,"sha256").hexdigest()!=expected:
                    raise ValueError("备份内容在恢复过程中发生变化")
        store=Store(stage,recover_publications=False)
        store.recover_withdrawals(stage/"published")
        with store.db() as db:
            db.execute("DELETE FROM sessions")
            db.execute("UPDATE plans SET enabled=0,active_run=NULL,revision=revision+1")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_jobs'").fetchone():
                db.execute("UPDATE analysis_jobs SET status='interrupted',reason='备份恢复后需人工确认再提交，未自动调用模型' WHERE status IN ('queued','running')")
                db.execute("UPDATE assets SET document=json_set(document,'$.analysis_status','not_analyzed') WHERE json_extract(document,'$.analysis_status')='analyzing'")
        store.recover_runs()
        stage.rename(destination)
    finally:
        if stage.exists():
            if stage.resolve().parent!=destination.parent or not stage.name.startswith(".omni-restore-"):
                raise ValueError("恢复临时目录位置异常，未执行清理")
            shutil.rmtree(stage)
    return destination


def main():
    parser=argparse.ArgumentParser(description="Omni advertising workbench setup")
    parser.add_argument("command",choices=["init","backup","restore"])
    parser.add_argument("--root",default="data/ad-workbench")
    parser.add_argument("--username",default="admin")
    parser.add_argument("--role",choices=["viewer","operator","admin"],default="admin")
    parser.add_argument("--destination")
    args=parser.parse_args()
    if args.command=="init":
        print("账号已创建，登录信息仅保存在本机私有文件：",initialize(args.root,args.username,args.role))
    elif args.command=="backup":
        if not args.destination:
            parser.error("backup requires --destination")
        print("备份完成：",backup(args.root,args.destination))
    else:
        if not args.destination:
            parser.error("restore requires --destination")
        print("已恢复到新目录，计划全部暂停、旧会话失效：",restore(args.root,args.destination))


if __name__=="__main__":
    main()
