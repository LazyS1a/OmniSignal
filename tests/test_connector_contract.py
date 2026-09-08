from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnisignal.contracts import (
    CollectRequest,
    ConnectorSpec,
    ErrorAction,
    ErrorCategory,
    default_action,
)
from omnisignal.testing import FixtureConnector
from omnisignal.sdk import Connector


FIXTURE_DIR = Path(__file__).parents[1] / "examples" / "connectors"


def load_spec(name: str) -> ConnectorSpec:
    data = yaml.safe_load((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return ConnectorSpec.model_validate(data)


@pytest.mark.parametrize(
    "name",
    ["rest.yaml", "rss.yaml", "static_html.yaml", "browser.yaml", "websocket.yaml", "file.yaml", "authorized_hook.yaml"],
)
def test_reference_specs_are_valid(name: str) -> None:
    assert load_spec(name).spec_version == "1.0"


def test_reference_spec_ids_are_unique() -> None:
    ids = [load_spec(path.name).id for path in FIXTURE_DIR.glob("*.yaml")]
    assert len(ids) == len(set(ids))


def test_literal_secret_is_rejected_as_unknown_field() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "rest.yaml").read_text(encoding="utf-8"))
    data["auth"] = {"kind": "bearer", "secret_ref": "env/REST_TOKEN", "token": "do-not-store-this"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConnectorSpec.model_validate(data)


def test_prohibited_field_is_rejected() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "rest.yaml").read_text(encoding="utf-8"))
    data["field_allowlist"].append("session_cookie")
    with pytest.raises(ValidationError, match="prohibited fields"):
        ConnectorSpec.model_validate(data)


def test_authorized_hook_requires_isolation_and_kill_switch() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "authorized_hook.yaml").read_text(encoding="utf-8"))
    data["isolation"] = {"mode": "in_process", "kill_switch": False}
    with pytest.raises(ValidationError, match="cannot run in process"):
        ConnectorSpec.model_validate(data)


def test_infinite_scroll_is_browser_only() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "rest.yaml").read_text(encoding="utf-8"))
    data["pagination"]["kind"] = "infinite_scroll"
    with pytest.raises(ValidationError, match="only valid for browser"):
        ConnectorSpec.model_validate(data)


def test_inline_credentials_are_rejected() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "rest.yaml").read_text(encoding="utf-8"))
    data["allowed_targets"] = ["https://user:password@example.invalid/items"]
    with pytest.raises(ValidationError, match="inline credentials"):
        ConnectorSpec.model_validate(data)


def test_secret_query_parameters_are_rejected() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "rest.yaml").read_text(encoding="utf-8"))
    data["allowed_targets"] = ["https://example.invalid/items?api_key=do-not-store-this"]
    with pytest.raises(ValidationError, match="secret query parameters"):
        ConnectorSpec.model_validate(data)


def test_file_targets_must_stay_inside_connector_workspace() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "file.yaml").read_text(encoding="utf-8"))
    data["allowed_targets"] = ["../outside/export.jsonl"]
    with pytest.raises(ValidationError, match="cannot traverse parent directories"):
        ConnectorSpec.model_validate(data)


def test_unsupported_spec_major_version_is_rejected() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "rest.yaml").read_text(encoding="utf-8"))
    data["spec_version"] = "2.0"
    with pytest.raises(ValidationError, match="String should match pattern"):
        ConnectorSpec.model_validate(data)


def test_checked_in_json_schema_matches_runtime_contract() -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "connector-spec.schema.json"
    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))
    assert checked_in == ConnectorSpec.model_json_schema()


def test_fixture_connector_resumes_from_checkpoint_and_replays_deterministically() -> None:
    async def scenario() -> None:
        spec = load_spec("rest.yaml")
        records = [{"id": "a", "text": "one"}, {"id": "b", "text": "two"}, {"id": "c", "text": "three"}]
        connector = FixtureConnector(spec, records)
        assert isinstance(connector, Connector)
        await connector.validate()

        first = await connector.collect(CollectRequest(run_id="run-001", limit=2))
        replay = await connector.collect(CollectRequest(run_id="run-002", limit=2))
        second = await connector.collect(CollectRequest(run_id="run-003", limit=2, checkpoint=first.next_checkpoint))

        assert [record.source_record_id for record in first.records] == ["a", "b"]
        assert [record.raw_hash for record in first.records] == [record.raw_hash for record in replay.records]
        assert [record.source_record_id for record in second.records] == ["c"]
        assert second.has_more is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("category", "action"),
    [
        (ErrorCategory.AUTHENTICATION, ErrorAction.PAUSE),
        (ErrorCategory.PERMISSION, ErrorAction.PAUSE),
        (ErrorCategory.RATE_LIMIT, ErrorAction.RETRY),
        (ErrorCategory.TRANSIENT_UPSTREAM, ErrorAction.RETRY),
        (ErrorCategory.SCHEMA_DRIFT, ErrorAction.QUARANTINE),
        (ErrorCategory.POLICY_VIOLATION, ErrorAction.DISABLE),
    ],
)
def test_default_error_actions(category: ErrorCategory, action: ErrorAction) -> None:
    assert default_action(category) == action
