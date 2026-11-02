"""Evaluation admission/replay tests use synthetic providers, never network."""
import importlib.util
import json
from pathlib import Path

import pytest

from omnisignal.workbench.store import Conflict
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


@pytest.mark.parametrize("max_calls", [0, 41, -1, True])
def test_submission_cap_is_explicit(tmp_path, max_calls):
    with pytest.raises(Conflict):
        evaluation.evaluate(tmp_path, allow_paid=True, max_calls=max_calls)
