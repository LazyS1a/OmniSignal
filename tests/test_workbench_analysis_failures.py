"""One mock HTTP request per failure; no real provider, retry, or production data."""
from copy import deepcopy
import json
import uuid

import httpx
import pytest

from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider
from omnisignal.workbench.store import Conflict
from test_workbench_analysis import prepared
from test_workbench_auto_editorial import SUGGESTION


PRIVATE = "synthetic-provider-secret-and-material-must-never-be-persisted"


def outcome(case, request):
    if isinstance(case, int):
        return httpx.Response(case, text=PRIVATE, headers={"Location": "https://other.invalid/"})
    if case == "timeout":
        raise httpx.ReadTimeout(PRIVATE, request=request)
    if case == "network":
        raise httpx.ConnectError(PRIVATE, request=request)
    if case == "unknown":
        raise RuntimeError(PRIVATE)
    if case == "oversized":
        return httpx.Response(200, content=(PRIVATE * 6000).encode())
    if case == "outer_json":
        return httpx.Response(200, text="{" + PRIVATE)
    if case == "envelope":
        return httpx.Response(200, json={"private_body": PRIVATE})
    if case == "content_type":
        return httpx.Response(200, json={"choices": [{"message": {"content": {"private_body": PRIVATE}}}]})
    if case == "inner_json":
        return httpx.Response(200, json={"choices": [{"message": {"content": "{" + PRIVATE}}]})
    value = deepcopy(SUGGESTION)
    value["description"] = PRIVATE
    if case == "taxonomy":
        value["taxonomy"]["tags"]["industry"] = [PRIVATE]
    elif case == "fields":
        value["kind"] = [PRIVATE]  # A malformed type must become a schema failure.
    else:
        raise AssertionError("unknown synthetic case")
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}}]})


@pytest.mark.parametrize("case,code", [
    (401, "upstream_authentication"), (403, "upstream_authentication"),
    (429, "upstream_rate_limited"), (500, "upstream_server_error"), (503, "upstream_server_error"),
    (400, "upstream_http_error"), (302, "upstream_http_error"),
    ("timeout", "upstream_timeout"), ("network", "upstream_network"),
    ("unknown", "unknown_error"), ("oversized", "response_too_large"),
    ("outer_json", "response_json_parse"), ("inner_json", "response_json_parse"),
    ("envelope", "response_schema_invalid"), ("content_type", "response_schema_invalid"),
    ("taxonomy", "response_schema_invalid"), ("fields", "response_schema_invalid"),
])
def test_safe_failure_classification_is_durable_single_shot(prepared, case, code):
    store, asset = prepared
    calls = []

    def handle(request):
        calls.append(request)
        return outcome(case, request)

    provider = HTTPAnalysisProvider(api_key=PRIVATE, model="qwen-synthetic-only",
                                    base_url="https://model.invalid/v1", transport=httpx.MockTransport(handle))
    service = AnalysisService(store, provider)
    original_job = service.enqueue(asset["id"], asset["revision"], "synthetic-admin")
    result = service.run_pending()
    assert result["id"] == original_job["id"] and result["status"] == "failed" and result["charged"]
    assert result["failure_code"] == code and result["suggestion"] is None
    assert not result["reason"].startswith("[") and "不会自动重试" in result["reason"]
    assert service.run_pending() is None and len(calls) == 1
    assert service.configuration()["used"] == 1
    with pytest.raises(Conflict, match="再次计费"):
        service.enqueue(asset["id"], asset["revision"], "synthetic-admin")
    assert len(calls) == 1
    with store.db() as db:
        row = db.execute("SELECT reason,suggestion FROM analysis_jobs WHERE id=?", (result["id"],)).fetchone()
        audits = [r[0] for r in db.execute("SELECT details FROM audit WHERE action='analysis_failed'")]
    assert row["reason"].startswith(f"[{code}] ") and row["suggestion"] is None
    assert PRIVATE not in row["reason"] + json.dumps(audits) + json.dumps(service.jobs())
    assert store.asset(asset["id"])["description"] == asset["description"]
    # A restart reads the stored code, does not reclassify via exception text or resubmit.
    restarted = AnalysisService(store, provider)
    assert restarted.jobs()[0]["failure_code"] == code and restarted.run_pending() is None
    assert len(calls) == 1 and restarted.configuration()["used"] == 1


def test_batch_view_exposes_code_without_technical_prefix_in_message(prepared):
    store, asset = prepared
    provider = HTTPAnalysisProvider(api_key=PRIVATE, model="qwen-synthetic-only",
                                    transport=httpx.MockTransport(lambda request: httpx.Response(429, text=PRIVATE)))
    service = AnalysisService(store, provider)
    batch = service.enqueue_batch(str(uuid.uuid4()), [{"asset_id": asset["id"], "expected_revision": asset["revision"]}], "synthetic-admin")
    service.run_pending()
    result = service.batch(batch["batch_id"], "synthetic-admin")
    assert result["items"][0]["failure_code"] == "upstream_rate_limited"
    assert not result["items"][0]["reason"].startswith("[")
    assert PRIVATE not in json.dumps(result)


def test_legacy_generic_failure_is_not_rewritten_or_guessed(prepared):
    store, asset = prepared
    provider = HTTPAnalysisProvider(api_key=PRIVATE, model="qwen-synthetic-only",
                                    transport=httpx.MockTransport(lambda request: pytest.fail("no request allowed")))
    service = AnalysisService(store, provider)
    job = service.enqueue(asset["id"], asset["revision"], "synthetic-admin")
    legacy = "分析失败或模型返回不合格；请求额度已计入，需人工确认后重试"
    with store.db() as db:
        db.execute("UPDATE analysis_jobs SET status='failed',reason=? WHERE id=?", (legacy, job["id"]))
    assert service.jobs()[0]["failure_code"] is None and service.jobs()[0]["reason"] == legacy
    assert AnalysisService(store, provider).run_pending() is None
    with store.db() as db:
        assert db.execute("SELECT reason FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone()[0] == legacy
