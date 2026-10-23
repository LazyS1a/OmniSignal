"""Read-only signed-package feed, with separate server-to-server identity."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from fastapi import APIRouter, Depends, HTTPException
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


def router(root):
    root=Path(root)
    bearer=HTTPBearer(auto_error=False)

    def authorize(credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
        expected=os.getenv('OMNISIGNAL_KNOWLEDGE_READ_TOKEN_SHA256','')
        if not re.fullmatch('[a-f0-9]{64}',expected):
            raise HTTPException(503,'下游读取身份尚未配置')
        if not credentials or not 32<=len(credentials.credentials)<=512 or not hmac.compare_digest(expected,hashlib.sha256(credentials.credentials.encode()).hexdigest()):
            raise HTTPException(401,'需要有效的下游读取身份')

    feed=APIRouter(prefix='/knowledge/v1',dependencies=[Depends(authorize)])

    def directory(release_id):
        if not isinstance(release_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',release_id):
            raise HTTPException(404,'发布版本不存在')
        return contained(root,'releases/'+release_id+'/manifest.json').parent

    @feed.get('/latest')
    def latest():
        if not (root/'latest.json').exists():
            return {'status':'empty','release':None}
        pointer=metadata(contained(root,'latest.json'))
        manifest=directory(pointer.get('release_id',''))/'manifest.json'
        if hashlib.sha256(manifest.read_bytes()).hexdigest()!=pointer.get('manifest_sha256'):
            raise HTTPException(503,'发布指针校验失败')
        return {'status':'published','release':pointer}

    @feed.get('/releases/{release_id}/manifest')
    def manifest(release_id:str):
        return FileResponse(contained(directory(release_id),'manifest.json'),media_type='application/json',headers={'Cache-Control':'no-store'})

    @feed.get('/releases/{release_id}/files/{file_path:path}')
    def file(release_id:str,file_path:str):
        folder=directory(release_id)
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
    def withdrawals():
        folder=root/'withdrawals'
        if folder.is_symlink():
            raise HTTPException(503,'撤回记录不可用')
        entries=[int(p.stem) for p in folder.glob('*.json') if p.stem.isdigit() and not p.is_symlink()]
        return {'status':'published','withdrawal':metadata(contained(root,'withdrawals/'+str(max(entries))+'.json'))} if entries else {'status':'empty','withdrawal':None}
    return feed
