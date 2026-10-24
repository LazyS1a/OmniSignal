"""Bounded client for Nova's authenticated inspiration-analysis job gateway.

Only a service token lives here. Upstream model credentials remain in Nova.
No automatic submission retries: an uncertain response may already be billed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .store import Conflict, encode, require


class NovaPending(Conflict):
    """Remote submission may exist; only read-only reconciliation is safe."""


class NovaAnalysisProvider:
    is_nova = True
    model_version = None

    def __init__(self, *, base_url, token, transport=None, clock=time.monotonic,
                 sleep=time.sleep, wait_seconds=100):
        try:
            parts = urlsplit(base_url)
            require(parts.scheme == "https" and bool(parts.hostname) and not parts.username
                    and not parts.password and parts.path in {"", "/"}
                    and not parts.query and not parts.fragment, "Nova 接口必须为 HTTPS 站点地址")
            parts.port
        except ValueError:
            raise Conflict("Nova 接口地址配置无效") from None
        require(isinstance(token, str) and 0 < len(token.strip()) <= 4096
                and "\r" not in token and "\n" not in token, "未配置 Nova 分析服务令牌")
        self.base_url = base_url.rstrip("/") + "/api/internal/inspiration-analysis"
        self.token, self.transport = token, transport
        self.clock, self.sleep = clock, sleep
        self.wait_seconds = min(max(float(wait_seconds), 1), 100)
        self._config = {"ready": False, "state": "not_checked", "model": None}
        self.cache_identity = hashlib.sha256(encode({"endpoint": self.base_url,
                                                     "contract": "design-v1"}).encode()).hexdigest()

    @classmethod
    def from_env(cls):
        return cls(base_url=os.getenv("OMNISIGNAL_WORKBENCH_NOVA_BASE_URL", ""),
                   token=os.getenv("OMNISIGNAL_WORKBENCH_NOVA_TOKEN", ""))

    def configuration(self):
        # UI refreshes never trigger network calls or expose endpoint/token.
        return dict(self._config)

    def _request(self, client, method, path, deadline, body=None):
        remaining = deadline - self.clock()
        require(remaining > 0, "Nova 分析等待超时")
        with client.stream(method, self.base_url + path, json=body,
                           timeout=httpx.Timeout(min(10, remaining)),
                           headers={"Authorization": "Bearer " + self.token}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                require(len(data) <= 256 * 1024 and self.clock() < deadline,
                        "Nova 分析响应超出限制")
        value = json.loads(data)
        require(isinstance(value, dict), "Nova 分析响应无效")
        return value

    def analyze(self, asset, image, receipt=None):
        submitted = bool(asset.get("_analysis_reconcile"))
        terminal = False
        try:
            request_id = str(uuid.UUID(asset["_analysis_request_id"]))
            deadline = self.clock() + self.wait_seconds
            with httpx.Client(follow_redirects=False, trust_env=False, transport=self.transport) as client:
                if submitted:
                    saved_id = asset.get("_analysis_remote_job_id")
                    require(saved_id is None or isinstance(saved_id, str) and 0 < len(saved_id) <= 128
                            and all(c.isascii() and (c.isalnum() or c in "-_") for c in saved_id),
                            "Nova 已存任务标识无效")
                    path = "/jobs/" + saved_id if saved_id else "/jobs/by-request/" + request_id
                    job = self._request(client, "GET", path, deadline)
                else:
                    require(len(image) <= 1024 * 1024, "Nova 分析图片超过限制")
                    config = self._request(client, "GET", "/config", deadline)
                    require(type(config.get("ready")) is bool, "Nova 配置响应无效")
                    model = config.get("model")
                    require(model is None or isinstance(model, str) and len(model) <= 200, "Nova 模型信息无效")
                    self._config = {"ready": config["ready"], "state": "ready" if config["ready"] else "unavailable",
                                    "model": model}
                    require(config["ready"], "Nova 图片分析尚未就绪")
                    body = {"request_id": request_id, "source_image_id": asset["id"],
                            "revision": asset["revision"], "image_sha256": asset["sha256"],
                            "image_base64": base64.b64encode(image).decode("ascii"),
                            "title": asset["title"][:500], "description": asset.get("description", "")[:2000]}
                    submitted = True
                    job = self._request(client, "POST", "/jobs", deadline, body)
                job_id = job.get("id")
                # Never interpolate arbitrary returned paths into authenticated requests.
                require(isinstance(job_id, str) and 0 < len(job_id) <= 128
                        and all(c.isascii() and (c.isalnum() or c in "-_") for c in job_id),
                        "Nova 任务标识无效")
                if receipt is not None:
                    receipt(job_id)
                while job.get("status") in {"queued", "running"}:
                    require(self.clock() + 2 < deadline, "Nova 分析等待超时")
                    self.sleep(2)
                    job = self._request(client, "GET", "/jobs/" + job_id, deadline)
                    require(job.get("id") == job_id, "Nova 返回了其他任务")
                terminal = job.get("status") in {"completed", "failed"}
                require(job.get("status") == "completed" and isinstance(job.get("result"), dict), "Nova 分析未完成")
                result = dict(job["result"])
                actual_model = result.pop("model_version", None)
                require(isinstance(actual_model, str) and 0 < len(actual_model.strip()) <= 200,
                        "Nova 分析缺少实际模型信息")
                # This metadata is issued by the authenticated gateway, not model JSON.
                self.model_version = actual_model
                return result
        except Exception as exc:
            # Never pass provider bodies, auth headers, or infrastructure errors to UI.
            if not asset.get("_analysis_reconcile") and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 401, 403, 409, 413, 422, 429}:
                raise Conflict("Nova 拒绝本次分析请求，请核验服务配置或额度后重试") from None
            if submitted and not terminal:
                raise NovaPending("Nova 任务结果尚未确认，后台只读同步原任务；不会重新提交或扣除新额度") from None
            raise Conflict("Nova 分析未完成或等待超时；可能已经计费，需人工确认后重试") from None
