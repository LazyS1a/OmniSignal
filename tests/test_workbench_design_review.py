"""Offline image-bound QA and signing acceptance; no paid calls or real assets."""
from copy import deepcopy
import json

import httpx
import pytest

from omnisignal.knowledge.release import verify_release
from omnisignal.workbench.analysis import HTTPAnalysisProvider
from omnisignal.workbench.design_skills import details_digest
from omnisignal.workbench.publishing import publish_store_assets
from omnisignal.workbench.review import apply_automated_review
from omnisignal.workbench.store import Conflict, encode
from test_workbench_auto_editorial import ACTOR, SUGGESTION, asset, complete, setup


def envelope(*, unknown=False, conflict=False):
    result = deepcopy(SUGGESTION)
    if unknown:
        result["reason"] += "包装具体材质无法确定。"
        result["taxonomy"]["note"] += "材质不确定不影响可见的版式。"
    observations = [{"id": f"o{index + 1}", "field": field}
                    for index, field in enumerate(list(result["design"])[:5])]
    details = {"profile": "packaging-analysis" if conflict else "static-ad-analysis",
               "route_reason": "画面以中央色块构成海报主体，四周留白。", "observations": observations,
               "source_statements": [],
               "inferences": [{"field": field, "evidence_ids": ["o1", "o3"]}
                              for field in list(result["design"])[5:]],
               "unknowns": ["不能由图像确定印刷材质与真实传播效果。"],
               "inspiration_card": {"task": "用单个色块和留白组织测试海报。", "evidence_ids": ["o1", "o3"]},
               "self_check": []}
    return {"result": result, "details": details}


def analyzed(setup, *, publish=True, unknown=False, conflict=False):
    store, analysis, auto, _, key = setup
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(envelope(unknown=unknown, conflict=conflict), ensure_ascii=False)}}]})
    analysis.provider = HTTPAnalysisProvider(api_key="synthetic-only", model="qwen-synthetic-offline",
        transport=httpx.MockTransport(handle), design_skills=True)
    item, job = complete(store, analysis, auto, asset(store, publish=publish))
    assert analysis.jobs(item["id"])[0]["status"] == "completed"
    return store, analysis, auto, key, item, job, calls


def test_known_design_with_local_unknowns_is_ai_reviewed_and_signed(setup):
    store, _, auto, key, item, job, calls = analyzed(setup, unknown=True)
    assert item["analysis_details_status"] == "current"
    assert item["processing_group"] == "ready"
    receipt = auto.tick()
    assert receipt["reviewed"] == receipt["published"] == 1
    current = store.asset(item["id"])
    assert current["analysis_details_status"] == "current"
    assert current["analysis_quality"]["status"] == "passed"
    assert current["analysis_quality_review"]["source_revision"] == item["revision"]
    assert current["analysis_quality_review"]["accepted_revision"] == current["revision"]
    assert current["processing_group"] == "published"
    _, records = verify_release(store.root / "published" / "releases" / receipt["releases"][0]["version"],
                                {"synthetic-key": key.public_key()})
    assert records[0]["case"]["editorial"]["reviewed_analysis"]["source_job_id"] == job["id"]
    assert receipt["human_reviewed"] is None and receipt["receipt_status"] == "unconfirmed"
    assert auto.tick()["published"] == 0 and len(calls) == 1


@pytest.mark.parametrize("alter", ["delete", "malformed", "edit"])
def test_v2_never_falls_back_to_legacy_when_details_missing_or_changed(setup, alter):
    store, _, auto, _, item, job, calls = analyzed(setup)
    with store.db() as db:
        if alter == "delete":
            db.execute("DELETE FROM analysis_details WHERE job_id=?", (job["id"],))
        else:
            document = json.loads(db.execute("SELECT document FROM analysis_details WHERE job_id=?", (job["id"],)).fetchone()[0])
            document = {"profile": "broken"} if alter == "malformed" else {**document, "unknowns": ["篡改未经任务绑定的结论。"]}
            db.execute("UPDATE analysis_details SET document=? WHERE job_id=?", (encode(document), job["id"]))
    tick = auto.tick()
    assert tick["reviewed"] == tick["published"] == 0
    assert tick["blocked_reasons"]["content_quality_attention"] == 1
    assert auto.tick()["reviewed"] == 0 and len(calls) == 1
    assert store.asset(item["id"])["status"] == "pending"
    assert not (store.root / "published" / "latest.json").exists()


def test_structured_content_conflict_is_held_without_claiming_human_work(setup):
    store, _, auto, _, item, job, calls = analyzed(setup, conflict=True)
    assert item["processing_group"] == "quality_issue"
    assert item["analysis_quality"]["issues"][0]["code"] == "profile_taxonomy_conflict"
    for _ in range(3):
        assert auto.tick()["blocked_reasons"]["content_quality_attention"] == 1
    with store.db() as db:
        outcome = db.execute("SELECT outcome,reason FROM auto_editorial_outcomes WHERE job_id=?", (job["id"],)).fetchone()
    assert outcome["outcome"] == "exception_held" and len(calls) == 1
    assert auto.status()["human_reviewed"] is None


def test_ai_review_does_not_grant_display_or_export_rights(setup):
    store, _, auto, _, item, _, calls = analyzed(setup, publish=False)
    tick = auto.tick()
    assert tick["reviewed"] == 1 and tick["published"] == 0
    assert tick["blocked_reasons"]["display_export_permission_required"] == 1
    assert store.asset(item["id"])["permissions"]["export"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("alter", ["details", "proof", "job"])
@pytest.mark.parametrize("automated", [True, False])
def test_publication_rechecks_quality_binding_after_acceptance(setup, alter, automated):
    store, _, _, _, item, job, calls = analyzed(setup)
    accepted = apply_automated_review(store, item["id"], item["revision"], job["id"])
    with store.db() as db:
        if alter == "details":
            document = json.loads(db.execute("SELECT document FROM analysis_details WHERE job_id=?", (job["id"],)).fetchone()[0])
            document["route_reason"] += "更改绑定内容。"
            db.execute("UPDATE analysis_details SET document=? WHERE job_id=?", (encode(document), job["id"]))
            # Even a recomputed job digest must disagree with the frozen acceptance proof.
            db.execute("UPDATE analysis_jobs SET details_sha256=? WHERE id=?", (details_digest(document), job["id"]))
        elif alter == "proof":
            raw = json.loads(db.execute("SELECT document FROM assets WHERE id=?", (item["id"],)).fetchone()[0])
            raw["analysis_quality_review"]["accepted_revision"] += 1
            db.execute("UPDATE assets SET document=? WHERE id=?", (encode(raw), item["id"]))
        else:
            db.execute("DELETE FROM analysis_jobs WHERE id=?", (job["id"],))
    with pytest.raises(Conflict):
        publish_store_assets(store, [item["id"]], ACTOR if automated else "synthetic-admin",
                             expected_revisions={item["id"]: accepted["revision"]}, automated_only=automated)
    assert not (store.root / "published" / "latest.json").exists() and len(calls) == 1


def test_source_edit_marks_card_stale_and_blocks_autopublication(setup):
    store, _, auto, _, item, job, calls = analyzed(setup)
    accepted = apply_automated_review(store, item["id"], item["revision"], job["id"])
    current = store.review(item["id"], {"expected_revision": accepted["revision"], "description": "重新编辑了来源说明。"}, "editor", "operator")
    assert current["analysis_details_status"] == "stale" and current["processing_group"] == "stale"
    assert auto.tick()["published"] == 0 and len(calls) == 1


def test_restored_same_bound_details_rechecks_locally_without_new_model_call(setup):
    store, _, auto, _, item, job, calls = analyzed(setup)
    with store.db() as db:
        original = db.execute("SELECT document FROM analysis_details WHERE job_id=?", (job["id"],)).fetchone()[0]
        db.execute("DELETE FROM analysis_details WHERE job_id=?", (job["id"],))
    assert auto.tick()["blocked_reasons"]["content_quality_attention"] == 1
    assert auto.tick()["reviewed"] == 0
    with store.db() as db:
        # Restore the exact original, digest-bound response; no new model result.
        db.execute("INSERT INTO analysis_details(job_id,document) VALUES(?,?)", (job["id"], original))
    assert auto.tick()["published"] == 1
    assert store.asset(item["id"])["analysis_details_status"] == "current" and len(calls) == 1
