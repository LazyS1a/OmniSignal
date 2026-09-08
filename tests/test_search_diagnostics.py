from __future__ import annotations

import asyncio
import httpx
import pytest
from streamlit.testing.v1 import AppTest

from omnisignal.connectors import searxng_worker
from test_searxng_results import load_policy, worker_job, FIXED_NOW, load_spec
from omnisignal.connectors.searxng_results import SearXNGResultsConnector
from omnisignal.governance import load_source_approval
from test_searxng_results import ROOT
from omnisignal.contracts import ConnectorFailure


def test_captcha_plus_healthy_engine_has_safe_diagnostics(tmp_path):
    policy = load_policy(queries=["test"], engines=["duckduckgo", "brave"])
    def request(endpoint, params, timeout):
        if params["engines"] == "duckduckgo":
            return {"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA secret-private-error"]]}
        return {"results": [{"url": "https://example.org/a", "title": "test", "content": "test", "engine": "brave"}]}
    result = searxng_worker.collect(worker_job(policy), now=FIXED_NOW, request_page=request)
    assert result["diagnostics"] == [
        {"query": "test", "engine": "duckduckgo", "record_count": 0, "status": "failed", "reason": "captcha"},
        {"query": "test", "engine": "brave", "record_count": 1, "status": "complete", "reason": None}]
    assert "secret-private" not in str(result)
    spec = load_spec()
    connector = SearXNGResultsConnector(spec, policy, load_source_approval(ROOT / "governance/source_registry.yaml", spec), workspace=tmp_path)
    connector._build_batch(result, 200)
    assert connector.last_diagnostics == result["diagnostics"]
    result["diagnostics"][0]["query"] = []
    with pytest.raises(ConnectorFailure):
        connector._build_batch(result, 200)


@pytest.mark.parametrize("reason,expected", [("CAPTCHA", "captcha"), ("HTTP 429", "rate_limit"),
                                             ("ReadTimeout", "timeout"), ("HTTP 403", "access_denied"),
                                             ("unknown private detail", "upstream")])
def test_upstream_reason_mapping(reason, expected):
    assert searxng_worker._engine_reason([["duckduckgo", reason]], "duckduckgo") == expected


def test_normal_empty_is_not_failure():
    result = searxng_worker.collect(worker_job(load_policy(queries=["test"])), now=FIXED_NOW,
                                   request_page=lambda *args: {"results": []})
    assert result["diagnostics"][0]["status"] == "empty"
    assert result["diagnostics"][0]["reason"] is None


def test_timeout_diagnostics_survive_error_envelope(tmp_path):
    policy = load_policy(queries=["test"])
    def timeout(*args):
        raise httpx.ReadTimeout("private error")
    result = searxng_worker.collect(worker_job(policy), now=FIXED_NOW, request_page=timeout)
    assert result["status"] == "error"
    assert result["diagnostics"][0]["reason"] == "timeout"
    spec = load_spec()
    connector = SearXNGResultsConnector(spec, policy, load_source_approval(ROOT / "governance/source_registry.yaml", spec), workspace=tmp_path)
    with pytest.raises(ConnectorFailure):
        connector._build_batch(result, 200)
    assert connector.last_diagnostics[0]["status"] == "failed"


def diagnostic_ui():
    from omnisignal.ops_ui.collection import render_collection
    def load(path, params):
        if path.endswith("collection-jobs"):
            return {"items": [{"job_id": "test", "source_id": "searxng_results", "status": "succeeded",
                    "result_summary": {"coverage_status": "partial", "diagnostics": [
                    {"query": "test", "engine": "duckduckgo", "record_count": 0, "status": "failed", "reason": "captcha"}]}}]}
        return {"items": [], "auto_start": False}
    render_collection(load, run=None)


def test_partial_success_ui():
    app = AppTest.from_function(diagnostic_ui).run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["状态"] == "部分成功"
    assert app.dataframe[1].value.iloc[0]["原因"] == "验证码限制"
