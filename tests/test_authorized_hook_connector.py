from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omnisignal.connectors import AuthorizedHookConnector, AuthorizedHookPolicy, FileRawResponseArchive
from omnisignal.connectors.durable import run_connector_durably
from omnisignal.contracts import CollectRequest, ConnectorFailure, ConnectorSpec, ErrorCategory, HealthStatus
from omnisignal.governance import HookSourceApproval, load_hook_source_approval
from omnisignal.storage import (
    Base,
    IngestedRecord,
    IngestionRun,
    RunStatus,
    SourceControlState,
    create_database_engine,
)


PROJECT_ROOT = Path(__file__).parents[1]
SPEC_PATH = PROJECT_ROOT / "examples" / "connectors" / "authorized_hook.yaml"
REGISTRY_PATH = PROJECT_ROOT / "governance" / "source_registry.yaml"


def make_connector(tmp_path: Path, mode: str = "ok", *, threshold: int = 2) -> AuthorizedHookConnector:
    spec = ConnectorSpec.model_validate(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))
    approval = load_hook_source_approval(REGISTRY_PATH, spec)
    policy = AuthorizedHookPolicy(
        max_output_bytes=1_000_000,
        circuit_failure_threshold=threshold,
        runner_options={"mode": mode},
    )
    return AuthorizedHookConnector(
        spec,
        policy,
        approval,
        workspace=tmp_path / "worker",
        archive=FileRawResponseArchive(tmp_path / "archive"),
    )


def test_registry_rejects_expired_hook_authorization(tmp_path: Path) -> None:
    spec = ConnectorSpec.model_validate(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))
    registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    source = next(item for item in registry["sources"] if item["id"] == spec.id)
    source["authorization_expires_at"] = "2020-01-01T00:00:00+00:00"
    expired = tmp_path / "registry.yaml"
    expired.write_text(yaml.safe_dump(registry, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConnectorFailure) as raised:
        load_hook_source_approval(expired, spec)
    assert raised.value.category == ErrorCategory.PERMISSION


def test_runner_hash_is_verified_before_process_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        spec = ConnectorSpec.model_validate(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))
        approval = load_hook_source_approval(REGISTRY_PATH, spec)
        tampered: HookSourceApproval = replace(approval, runner_sha256="0" * 64)
        connector = AuthorizedHookConnector(
            spec,
            AuthorizedHookPolicy(),
            tampered,
            workspace=tmp_path / "worker",
        )
        with pytest.raises(ConnectorFailure) as raised:
            await connector.validate()
        assert raised.value.category == ErrorCategory.POLICY_VIOLATION

    asyncio.run(scenario())


def test_hook_worker_paginates_and_durable_replay_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "hook.db"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        runs = []
        for _ in range(4):
            runs.append(
                await run_connector_durably(
                    make_connector(tmp_path),
                    database_url=database_url,
                    database_target="sqlite+pysqlite://local/hook.db",
                    initialize_schema=True,
                )
            )
        assert [run.inserted for run in runs] == [1, 1, 0, 0]
        assert [run.unchanged for run in runs] == [0, 0, 1, 1]
        assert [run.cycle_complete for run in runs] == [False, True, False, True]
        engine = create_database_engine(database_url)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(IngestedRecord)) == 2
            assert all(record.raw_archive_sha256 for record in session.scalars(select(IngestedRecord)))
            stored_runs = list(session.scalars(select(IngestionRun).order_by(IngestionRun.created_at)))
            assert [run.status for run in stored_runs] == [RunStatus.SUCCEEDED] * 4
        engine.dispose()

    asyncio.run(scenario())
    archives = list((tmp_path / "archive").rglob("*.json.gz"))
    assert len(archives) == 2


def test_durable_runner_refuses_disabled_source_before_creating_run(tmp_path: Path) -> None:
    database_path = tmp_path / "disabled-hook.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            SourceControlState(
                source_id="fixture_hook",
                enabled=False,
                version=1,
                updated_by="ops-one",
                reason="planned maintenance",
            )
        )
        session.commit()
    engine.dispose()

    async def scenario() -> None:
        with pytest.raises(ConnectorFailure) as raised:
            await run_connector_durably(
                make_connector(tmp_path),
                database_url=database_url,
                database_target="sqlite+pysqlite://local/disabled-hook.db",
                initialize_schema=False,
            )
        assert raised.value.category == ErrorCategory.POLICY_VIOLATION

    asyncio.run(scenario())
    engine = create_database_engine(database_url)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    engine.dispose()


@pytest.mark.parametrize(
    ("mode", "category"),
    [
        ("unauthorized", ErrorCategory.AUTHENTICATION),
        ("forbidden", ErrorCategory.PERMISSION),
        ("schema_drift", ErrorCategory.SCHEMA_DRIFT),
        ("wrong_target", ErrorCategory.POLICY_VIOLATION),
        ("sensitive_field", ErrorCategory.POLICY_VIOLATION),
    ],
)
def test_hook_boundary_failures_open_only_its_circuit(mode: str, category: ErrorCategory, tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = make_connector(tmp_path, mode)
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id=f"hook-{mode}", limit=1))
        assert raised.value.category == category
        health = await connector.health()
        assert health.status == HealthStatus.PAUSED

    asyncio.run(scenario())


def test_runner_crash_threshold_persists_and_can_be_reset(tmp_path: Path) -> None:
    async def scenario() -> None:
        for attempt in range(2):
            connector = make_connector(tmp_path, "crash", threshold=2)
            with pytest.raises(ConnectorFailure) as raised:
                await connector.collect(CollectRequest(run_id=f"hook-crash-{attempt}", limit=1))
            assert raised.value.category == ErrorCategory.RUNNER_CRASH
        healthy_worker = make_connector(tmp_path, "ok", threshold=2)
        assert (await healthy_worker.health()).status == HealthStatus.PAUSED
        with pytest.raises(ConnectorFailure) as raised:
            await healthy_worker.collect(CollectRequest(run_id="hook-circuit-open", limit=1))
        assert raised.value.category == ErrorCategory.RESOURCE_EXHAUSTED
        healthy_worker.reset_circuit()
        batch = await healthy_worker.collect(CollectRequest(run_id="hook-after-reset", limit=1))
        assert len(batch.records) == 1

    asyncio.run(scenario())


def test_worker_receives_only_minimal_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("OMNISIGNAL_UNRELATED_SECRET", "must-not-reach-worker")
        connector = make_connector(tmp_path, "environment_probe")
        batch = await connector.collect(CollectRequest(run_id="hook-env-probe", limit=1))
        assert batch.records[0].payload["text"] == "minimal environment confirmed"

    asyncio.run(scenario())


def test_hook_timeout_terminates_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = make_connector(tmp_path, "hang")
        data = connector.spec.model_dump(mode="json")
        data["limits"]["timeout_seconds"] = 1
        connector.spec = ConnectorSpec.model_validate(data)
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id="hook-timeout", limit=1))
        assert raised.value.category == ErrorCategory.RUNNER_CRASH

    asyncio.run(scenario())
