"""Diagnostic durability and secrecy, using synthetic material only."""
import json

import pytest

from omnisignal.workbench.analysis import AnalysisService, FAILURE_REASONS
from omnisignal.workbench.schema_diagnostics import safe_diagnostic, schema_diagnostic
from test_workbench_design_skills import prepared, envelope, SkillsProvider

PRIVATE = "private-input-must-not-be-retained"


@pytest.mark.parametrize("field,expected", [
    ("taxonomy", {"code": "taxonomy_labels", "path": "result.taxonomy.tags"}),
    ("source_quote", {"code": "source_quote_unbound", "path": "details.source_statements[].source_quote"}),
    ("extra_key", {"code": "details_fields", "path": "details"}),
])
def test_failed_schema_keeps_only_fixed_diagnostic_and_never_retries(prepared, field, expected):
    store, asset = prepared
    value = envelope()
    if field == "taxonomy":
        value["result"]["taxonomy"]["tags"]["industry"] = [PRIVATE]
    elif field == "source_quote":
        value["details"]["source_statements"] = [{"text": PRIVATE, "source_quote": PRIVATE}]
    else:
        value["details"][PRIVATE] = PRIVATE
    provider = SkillsProvider(value)
    service = AnalysisService(store, provider)
    job = service.enqueue(asset["id"], asset["revision"], "synthetic-admin")
    failed = service.run_pending()
    assert failed["status"] == "failed" and failed["charged"]
    assert failed["failure_code"] == "response_schema_invalid"
    assert failed["failure_diagnostic"] == expected
    assert failed["suggestion"] is None and failed["details"] is None
    assert service.run_pending() is None and provider.calls == 1
    with store.db() as db:
        stored = dict(db.execute("SELECT * FROM analysis_jobs WHERE id=?", (job["id"],)).fetchone())
        assert json.loads(stored["failure_diagnostic"]) == expected
        assert PRIVATE not in json.dumps(stored)
    restarted = AnalysisService(store, provider)
    assert restarted.jobs()[0]["failure_diagnostic"] == expected
    assert restarted.run_pending() is None and provider.calls == 1
    assert store.asset(asset["id"])["permissions"]["display"] == "unknown"


def test_legacy_schema_failure_stays_unknown_and_unmodified(prepared):
    store, asset = prepared
    provider = SkillsProvider()
    service = AnalysisService(store, provider)
    job = service.enqueue(asset["id"], asset["revision"], "synthetic-admin")
    with store.db() as db:
        db.execute("UPDATE analysis_jobs SET status='failed',reason=? WHERE id=?",
                   (FAILURE_REASONS["response_schema_invalid"], job["id"]))
        # Match the deployed pre-diagnostic schema before opening a fresh service.
        db.execute("ALTER TABLE analysis_jobs DROP COLUMN failure_diagnostic")
    restarted = AnalysisService(store, provider)
    assert restarted.jobs()[0]["failure_diagnostic"] is None
    assert restarted.run_pending() is None and provider.calls == 0


@pytest.mark.parametrize("value", [PRIVATE, {"code": PRIVATE, "path": "result"},
    {"code": "taxonomy_labels", "path": PRIVATE}, {"code": [], "path": {}},
    {"code": "taxonomy_labels", "path": "result.taxonomy.tags", "value": PRIVATE}])
def test_read_side_rejects_unknown_or_injected_diagnostics(value):
    assert safe_diagnostic(value) is None


def test_unknown_exception_never_becomes_a_stored_message_or_path():
    assert schema_diagnostic(KeyError(PRIVATE), stage="details") == {
        "code": "details_validation_failed", "path": "details"}


def test_successful_analysis_has_no_failure_diagnostic(prepared):
    store, asset = prepared
    service = AnalysisService(store, SkillsProvider())
    service.enqueue(asset["id"], asset["revision"], "synthetic-admin")
    result = service.run_pending()
    assert result["status"] == "completed" and result["failure_diagnostic"] is None
