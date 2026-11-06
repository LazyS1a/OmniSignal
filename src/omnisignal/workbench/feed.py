"""Read-only signed-package feed, with separate server-to-server identity."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def contained(root, relative):
    if not relative or '\\' in relative or ':' in relative or any(p in ('','.','..') for p in relative.split('/')):
        raise HTTPException(404,'发布文件不存在')
    current=root
    if current.is_symlink():
        raise HTTPException(404,'发布文件不存在')
    for part in relative.split('/'):
        current=current/part
        if current.is_symlink():
            raise HTTPException(404,'发布文件不存在')
    if not current.resolve().is_relative_to(root.resolve()) or not current.is_file():
        raise HTTPException(404,'发布文件不存在')
    return current


def metadata(path):
    try:
        if path.stat().st_size > 4_000_000:
            raise ValueError()
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):
            raise ValueError()
        return value
    except (ValueError,OSError,RecursionError):
        raise HTTPException(503,'发布元数据不可用') from None


def router(root, *, prefix='/knowledge/v1', policy_loader=None):
    root=Path(root)
    bearer=HTTPBearer(auto_error=False)

    def authorize(credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
        expected=os.getenv('OMNISIGNAL_KNOWLEDGE_READ_TOKEN_SHA256','')
        if not re.fullmatch('[a-f0-9]{64}',expected):
            raise HTTPException(503,'下游读取身份尚未配置')
        if not credentials or not 32<=len(credentials.credentials)<=512 or not hmac.compare_digest(expected,hashlib.sha256(credentials.credentials.encode()).hexdigest()):
            raise HTTPException(401,'需要有效的下游读取身份')

    feed=APIRouter(prefix=prefix,dependencies=[Depends(authorize)])

    def context(request:Request):
        if policy_loader is None:
            return root,None
        from .publication_use import library_recipient
        from .library_publication import library_root, require_library_target
        try:
            config=policy_loader()
            policy=config.get('publication_authorized') if config else None
            recipient=library_recipient(policy)
            if recipient is None or recipient['library_id']!=request.path_params.get('library_id'):
                raise ValueError('unknown_library')
            selected=library_root(root,recipient)
            require_library_target(selected,policy)
            return selected,recipient
        except (ValueError,KeyError,TypeError,OSError):
            raise HTTPException(404,'灵感库发布源不存在') from None

    def scoped_metadata(path,recipient):
        from omnisignal.knowledge.scope import envelope_recipient
        value=metadata(path)
        try:
            if envelope_recipient(value)!=recipient:
                raise ValueError('recipient_mismatch')
        except (ValueError,TypeError):
            raise HTTPException(503,'发布接收范围不匹配') from None
        return value

    def scope_fields(recipient):
        return {'schema_version':2,'recipient':recipient} if recipient is not None else {}

    def directory(release_id,selected,recipient):
        if not isinstance(release_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',release_id):
            raise HTTPException(404,'发布版本不存在')
        path=contained(selected,'releases/'+release_id+'/manifest.json')
        scoped_metadata(path,recipient)
        return path.parent

    @feed.get('/latest')
    def latest(selected_context=Depends(context)):
        selected,recipient=selected_context
        if not (selected/'latest.json').exists():
            return {'status':'empty','release':None,**scope_fields(recipient)}
        pointer=metadata(contained(selected,'latest.json'))
        manifest=directory(pointer.get('release_id',''),selected,recipient)/'manifest.json'
        if hashlib.sha256(manifest.read_bytes()).hexdigest()!=pointer.get('manifest_sha256'):
            raise HTTPException(503,'发布指针校验失败')
        return {'status':'published','release':pointer,**scope_fields(recipient)}

    @feed.get('/releases/{release_id}/manifest')
    def manifest(release_id:str,selected_context=Depends(context)):
        return FileResponse(contained(directory(release_id,*selected_context),'manifest.json'),media_type='application/json',headers={'Cache-Control':'no-store'})

    @feed.get('/releases/{release_id}/files/{file_path:path}')
    def file(release_id:str,file_path:str,selected_context=Depends(context)):
        folder=directory(release_id,*selected_context)
        data=metadata(folder/'manifest.json')
        if not isinstance(data.get('files'),list):
            raise HTTPException(503,'发布清单无效')
        entry=next((x for x in data['files'] if isinstance(x,dict) and x.get('path')==file_path),None)
        if not entry:
            raise HTTPException(404,'文件不在发布清单中')
        path=contained(folder,file_path)
        if path.stat().st_size!=entry.get('size') or path.stat().st_size>512*1024*1024:
            raise HTTPException(503,'发布文件校验失败')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=entry.get('sha256'):
                raise HTTPException(503,'发布文件校验失败')
        return FileResponse(path,media_type='application/octet-stream',headers={'Cache-Control':'no-store'})

    @feed.get('/withdrawals/latest')
    def withdrawals(selected_context=Depends(context)):
        selected,recipient=selected_context
        folder=selected/'withdrawals'
        if folder.is_symlink():
            raise HTTPException(503,'撤回记录不可用')
        entries=[int(p.stem) for p in folder.glob('*.json') if p.stem.isdigit() and not p.is_symlink()]
        return {'status':'published','withdrawal':scoped_metadata(contained(selected,'withdrawals/'+str(max(entries))+'.json'),recipient),**scope_fields(recipient)} if entries else {'status':'empty','withdrawal':None,**scope_fields(recipient)}

    def snapshot_context(selected_context=Depends(context)):
        from omnisignal.knowledge import snapshot_v3
        selected,recipient=selected_context
        if not snapshot_v3.enabled() or recipient is None:
            raise HTTPException(404,'分片发布源未开放')
        return selected,recipient

    def snapshot_manifest(release_id,selected,recipient):
        from omnisignal.knowledge import snapshot_v3
        try:
            value=snapshot_v3.read_manifest(selected,release_id)
            if value['recipient']!=recipient:
                raise ValueError('recipient_mismatch')
            return value
        except (ValueError,TypeError,OSError):
            raise HTTPException(404,'分片发布版本不可用') from None

    @feed.get('/snapshots/latest')
    def snapshot_latest(selected_context=Depends(snapshot_context)):
        from omnisignal.knowledge import snapshot_v3
        selected,recipient=selected_context
        try:
            pointer=snapshot_v3.read_snapshot_latest(selected)
            if pointer is not None:
                snapshot_manifest(pointer['release_id'],selected,recipient)
            return {'schema_version':3,'recipient':recipient,'status':'published' if pointer else 'empty','release':pointer}
        except (ValueError,TypeError,OSError):
            raise HTTPException(503,'分片发布指针校验失败') from None

    @feed.get('/snapshots/releases/{release_id}/manifest')
    def snapshot_root_manifest(release_id:str,selected_context=Depends(snapshot_context)):
        from omnisignal.knowledge import snapshot_v3
        selected,recipient=selected_context
        snapshot_manifest(release_id,selected,recipient)
        path=contained(snapshot_v3.storage(selected),'releases/'+release_id+'/manifest.json')
        return FileResponse(path,media_type='application/json',headers={'Cache-Control':'no-store'})

    @feed.get('/snapshots/releases/{release_id}/{category}/{sha256}')
    def snapshot_content(release_id:str,category:str,sha256:str,selected_context=Depends(snapshot_context)):
        from omnisignal.knowledge import snapshot_v3
        selected,recipient=selected_context
        snapshot_manifest(release_id,selected,recipient)
        try:
            path=snapshot_v3.referenced_file(selected,release_id,category,sha256)
        except (ValueError,TypeError,OSError):
            raise HTTPException(404,'文件不属于该发布版本或校验失败') from None
        return FileResponse(path,media_type='application/octet-stream',headers={'Cache-Control':'private, immutable','ETag':'"'+sha256+'"'})
    return feed
