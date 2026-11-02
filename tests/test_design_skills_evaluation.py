"""Evaluation admission/replay tests use synthetic providers, never network."""
import importlib.util
import json
from pathlib import Path

import pytest
import httpx

from omnisignal.workbench.store import Conflict
from omnisignal.workbench.analysis import HTTPAnalysisProvider
from test_workbench_design_skills import envelope, SkillsProvider

spec = importlib.util.spec_from_file_location("design_evaluation", Path(__file__).resolve().parents[1] / "scripts/evaluate_design_skills.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class LegacyProvider(SkillsProvider):
    cache_identity = "synthetic-legacy-eval"

    def __init__(self):
        super().__init__(envelope()["result"])


def test_default_only_creates_twenty_bound_fixtures(tmp_path):
    def prohibited(_):
        raise AssertionError("Default must not instantiate a provider")
    report = evaluation.evaluate(tmp_path, factory=prohibited)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["samples"]) == 20
    assert len({x["sha256"] for x in manifest["samples"]}) == 20
    assert sum(x["split"] == "holdout" for x in manifest["samples"]) == 8
    assert report["reserved_submissions"] == 0
    assert report["human_quality_reviewed"] is None and report["cost"] is None
    assert all(x["rights"] == "self_owned_fixture" for x in manifest["samples"])
    assert evaluation.evaluate(tmp_path, factory=prohibited)["reserved_submissions"] == 0


def test_two_lanes_are_new_tasks_and_replay_never_retries(tmp_path, monkeypatch):
    legacy, skills = LegacyProvider(), SkillsProvider()
    factory = lambda lane: skills if lane == "skills" else legacy
    monkeypatch.setenv("OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT", "120")
    report = evaluation.evaluate(tmp_path, allow_paid=True, max_calls=4, factory=factory)
    assert report["reserved_submissions"] == 4 and report["states"]["completed"] == 4
    assert legacy.calls == skills.calls == 2
    assert all(report["cases"][0]["runs"][lane] is not None for lane in ("legacy", "skills"))
    assert __import__("os").environ["OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT"] == "120"
    second = evaluation.evaluate(tmp_path, allow_paid=True, max_calls=4, factory=factory)
    assert second["states"] == report["states"]
    assert legacy.calls == skills.calls == 2


def test_failed_submission_is_not_replayed(tmp_path):
    legacy, skills = LegacyProvider(), SkillsProvider({"result": {}, "details": {}})
    factory = lambda lane: skills if lane == "skills" else legacy
    report = evaluation.evaluate(tmp_path, allow_paid=True, max_calls=2, factory=factory)
    assert report["states"]["failed"] == 1 and report["states"]["completed"] == 1
    evaluation.evaluate(tmp_path, allow_paid=True, max_calls=2, factory=factory)
    assert legacy.calls == skills.calls == 1


def test_changed_fixture_or_configuration_stops_without_new_calls(tmp_path):
    legacy, skills = LegacyProvider(), SkillsProvider()
    factory = lambda lane: skills if lane == "skills" else legacy
    evaluation.evaluate(tmp_path, allow_paid=True, max_calls=2, factory=factory)
    skills.cache_identity = "design-v1:changed"
    with pytest.raises(Conflict):
        evaluation.evaluate(tmp_path, allow_paid=True, max_calls=4, factory=factory)
    assert legacy.calls == skills.calls == 1
    (tmp_path / "images/sample-01.png").write_bytes(b"modified")
    with pytest.raises(Conflict):
        evaluation.evaluate(tmp_path)


def test_uncertain_submission_and_lost_ledger_never_submit_again(tmp_path):
    legacy, skills = LegacyProvider(), SkillsProvider()
    factory = lambda lane: skills if lane == "skills" else legacy
    evaluation.evaluate(tmp_path, allow_paid=True, max_calls=2, factory=factory)
    ledger_path = tmp_path / "ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["attempts"]["sample-01:legacy"]["status"] = "queued"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    report = evaluation.evaluate(tmp_path, allow_paid=True, max_calls=4, factory=factory)
    assert report["uncertain_submission_blocks_new_calls"] is True
    assert legacy.calls == skills.calls == 1
    ledger_path.unlink()
    with pytest.raises(Conflict):
        evaluation.evaluate(tmp_path, allow_paid=True, factory=factory)
    assert legacy.calls == skills.calls == 1


def test_two_schema_failures_stop_and_stay_stopped(tmp_path):
    provider = SkillsProvider({"wrong": "schema"})
    report = evaluation.evaluate(tmp_path, allow_paid=True, selected_lanes=("skills",), factory=lambda _: provider)
    assert provider.calls == 2
    assert report["stopped_reason"] == "two_consecutive_skills_schema_failures"
    assert report["reserved_submissions"] == 2
    evaluation.evaluate(tmp_path, allow_paid=True, selected_lanes=("skills",), factory=lambda _: provider)
    assert provider.calls == 2


def test_skills_only_reuses_existing_paid_baseline_read_only(tmp_path):
    baseline = tmp_path / "baseline"
    legacy, skills = LegacyProvider(), SkillsProvider()
    evaluation.evaluate(baseline, allow_paid=True, max_calls=2, factory=lambda lane: skills if lane == "skills" else legacy)
    original = (baseline / "ledger.json").read_bytes()
    current = SkillsProvider()
    report = evaluation.evaluate(tmp_path / "v2", allow_paid=True, selected_lanes=("skills",), max_calls=1,
                                 baseline=baseline, factory=lambda lane: current if lane == "skills" else pytest.fail("No baseline calls allowed"))
    assert current.calls == 1 and legacy.calls == skills.calls == 1
    assert report["baseline_comparison"]["completed"] == 1
    assert report["baseline_comparison"]["additional_calls"] == 0
    assert report["baseline_comparison"]["missing_or_incomplete"] == 19
    assert (baseline / "ledger.json").read_bytes() == original


def test_private_capture_diagnoses_locally_without_second_call(tmp_path):
    value = envelope()
    value["details"]["source_statements"] = [{"text": "来源声称获奖", "source_quote": "并不存在的引文"}]
    submissions = []
    def respond(request):
        submissions.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]})
    provider = HTTPAnalysisProvider(api_key="synthetic-only", model="qwen-test", design_skills=True, transport=httpx.MockTransport(respond))
    report = evaluation.evaluate(tmp_path, allow_paid=True, selected_lanes=("skills",), max_calls=1, factory=lambda _: provider)
    assert len(submissions) == 1
    attempt = report["cases"][0]["runs"]["skills"]
    assert attempt["status"] == "failed"
    assert attempt["diagnostic"] == {"stage": "details", "reason": "来源引文不在当前来源资料内"}
    assert (tmp_path / "sample-01-skills.raw.json").is_file()
    assert "并不存在的引文" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("max_calls", [0, 41, -1, True])
def test_submission_cap_is_explicit(tmp_path, max_calls):
    with pytest.raises(Conflict):
        evaluation.evaluate(tmp_path, allow_paid=True, max_calls=max_calls)
