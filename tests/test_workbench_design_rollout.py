"""Rollout is a durable admission budget, never a reason to double-charge."""
import io
import json

import httpx
from PIL import Image

from omnisignal.workbench.analysis import AnalysisService, HTTPAnalysisProvider
from omnisignal.workbench.store import Store
from test_workbench_design_skills import envelope


def test_versioned_budget_falls_back_once_and_keeps_cache_on_lane_change(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "unlimited")
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_DESIGN_SKILLS_DAILY_LIMIT", "2")
    calls = []
    def transport(request):
        body = json.loads(request.content)
        value = envelope() if body["max_tokens"] == 10000 else envelope()["result"]
        calls.append(body["max_tokens"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]})
    provider = HTTPAnalysisProvider(api_key="fixture-only", model="qwen-fixture", design_skills=True,
                                    transport=httpx.MockTransport(transport))
    store = Store(tmp_path / "store")
    service = AnalysisService(store, provider)
    assets = []
    for index in range(3):
        image = io.BytesIO()
        Image.new("RGB", (120, 120), (index * 50, 90, 10)).save(image, "PNG")
        asset = store.import_asset({"title": "自有合成测试", "source_url": "https://example.invalid/fixture"}, image.getvalue())
        asset = store.review(asset["id"], {"expected_revision": 1,
            "permissions": {"analysis": "allowed", "display": "unknown", "export": "unknown"},
            "permission_note": "隔离夹具"}, "test", "admin")
        assets.append(asset)
    batch = service.enqueue_batch("d47fd181-205b-4a9d-9e85-7deab5f24643",
        [{"asset_id": a["id"], "expected_revision": a["revision"]} for a in assets], "test")
    assert batch["totals"]["submitted"] == 3
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM analysis_jobs WHERE provider LIKE 'design-v1:%'").fetchone()[0] == 2
    # A restart retains reservations; it cannot reset the rollout budget.
    service = AnalysisService(store, provider)
    assert service.configuration()["design_skills"]["used_and_reserved"] == 2
    jobs = [service.run_pending() for _ in range(3)]
    assert all(job["status"] == "completed" for job in jobs)
    assert sorted(calls) == [6000, 10000, 10000]
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_DESIGN_SKILLS_DAILY_LIMIT", "0")
    cached = service.enqueue(assets[0]["id"], assets[0]["revision"], "test")
    assert cached["cached"] and len(calls) == 3
    assert service.run_pending() is None
