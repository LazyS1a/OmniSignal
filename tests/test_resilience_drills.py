"""Fault injection against the real durable runner, without external traffic."""

import asyncio
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnisignal.connectors.durable import run_connector_durably
from omnisignal.contracts import ConnectorSpec, ConnectorFailure, ErrorCategory
from omnisignal.storage import AuditEvent, ConnectorState, IngestedRecord, IngestionRun, RunStatus, create_database_engine
from omnisignal.testing import FixtureConnector


class InterruptedFixture(FixtureConnector):
    def __init__(self, spec, records, failure, stage):
        super().__init__(spec, records)
        self.failure, self.stage = failure, stage
        self.collect_calls = 0

    async def collect(self, request):
        self.collect_calls += 1
        if self.collect_calls == 2 and self.stage == "collect":
            raise self.failure
        return await super().collect(request)

    async def sync_deletions(self, checkpoint=None):
        if self.collect_calls == 2 and self.stage == "deletions":
            raise self.failure
        return await super().sync_deletions(checkpoint)


@pytest.mark.parametrize("stage", ["collect", "deletions"])
@pytest.mark.parametrize("failure_kind", ["network", "unexpected", "cancelled"])
def test_interrupted_run_resumes_last_committed_batch(tmp_path: Path, stage: str, failure_kind: str):
    spec_data = yaml.safe_load((Path(__file__).parents[1] / "examples/connectors/rest.yaml").read_text(encoding="utf-8"))
    spec_data["pagination"].update(page_size=1, max_pages=10)
    spec = ConnectorSpec.model_validate(spec_data)
    records = [{"id": str(index), "text": f"fixture {index}"} for index in range(3)]
    failure = {
        "network": ConnectorFailure(ErrorCategory.TRANSIENT_UPSTREAM, "sensitive-fixture-error"),
        "unexpected": RuntimeError("sensitive-fixture-error"),
        "cancelled": asyncio.CancelledError("sensitive-fixture-error"),
    }[failure_kind]
    url = f"sqlite+pysqlite:///{(tmp_path / 'resume.db').as_posix()}"
    options = dict(database_url=url, database_target="sqlite://local/resume.db", initialize_schema=True)
    broken = InterruptedFixture(spec, records, failure, stage)
    with pytest.raises(type(failure)):
        asyncio.run(run_connector_durably(broken, **options))
    assert broken._closed
    engine = create_database_engine(url)
    try:
        with Session(engine) as session:
            assert session.get(ConnectorState, spec.id).checkpoint == {"offset": 1}
            assert len(session.scalars(select(IngestedRecord)).all()) == 1
            run = session.scalar(select(IngestionRun))
            assert run.status == RunStatus.FAILED
            assert run.records_written == 1
            assert run.finished_at is not None
            assert "sensitive-fixture-error" not in (run.error_summary or "")
            events = session.scalars(select(AuditEvent).where(AuditEvent.action == "connector_run_stopped")).all()
            assert len(events) == 1
            assert events[0].detail["category"] == ("transient_upstream" if failure_kind == "network" else "runner_crash")
        resumed = asyncio.run(run_connector_durably(FixtureConnector(spec, records), **options))
        assert resumed.inserted == 2
        replay = asyncio.run(run_connector_durably(FixtureConnector(spec, records), **options))
        assert replay.inserted == replay.updated == 0
        with Session(engine) as session:
            assert session.get(ConnectorState, spec.id).checkpoint == {"offset": 3}
            assert {r.source_record_id for r in session.scalars(select(IngestedRecord))} == {"0", "1", "2"}
    finally:
        engine.dispose()


def test_engine_disposed_even_if_connector_close_fails(tmp_path: Path, monkeypatch):
    from omnisignal.connectors import durable

    spec_data = yaml.safe_load((Path(__file__).parents[1] / "examples/connectors/rest.yaml").read_text(encoding="utf-8"))
    spec = ConnectorSpec.model_validate(spec_data)

    class BrokenClose(FixtureConnector):
        async def close(self):
            raise RuntimeError("fixture close failure")

    url = f"sqlite+pysqlite:///{(tmp_path / 'close.db').as_posix()}"
    engine = create_database_engine(url)
    disposed = []
    original_dispose = engine.dispose

    def dispose():
        disposed.append(True)
        original_dispose()

    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(durable, "create_database_engine", lambda _: engine)
    with pytest.raises(RuntimeError, match="fixture close failure"):
        asyncio.run(run_connector_durably(BrokenClose(spec, []), database_url=url, database_target="sqlite://local/close.db", initialize_schema=True))
    assert disposed == [True]
