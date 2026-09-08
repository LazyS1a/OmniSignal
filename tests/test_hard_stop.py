import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
import yaml

from omnisignal.connectors.durable import run_connector_durably
from omnisignal.contracts import ConnectorSpec
from omnisignal.storage import AuditEvent, ConnectorState, IngestedRecord, IngestionRun, RunStatus, create_database_engine, upgrade_database
from omnisignal.testing import FixtureConnector


@pytest.mark.parametrize("stage", ["after_commit", "before_commit"])
@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_hard_kill_preserves_commits_and_resumes_without_duplicates(tmp_path: Path, stage: str, journal: str):
    root = Path(__file__).parents[1]
    database, marker = tmp_path / "hard-stop.db", tmp_path / "ready"
    url = f"sqlite+pysqlite:///{database.as_posix()}"
    upgrade_database(url)
    with sqlite3.connect(database) as connection:
        assert connection.execute(f"PRAGMA journal_mode={journal}").fetchone()[0].upper() == journal
    child_env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC"}}
    child_env["PYTHONPATH"] = str(root / "src")
    child = subprocess.Popen(
        [sys.executable, str(root / "tests/fixtures/hard_stop_worker.py"), str(database), str(marker), stage],
        cwd=root, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "owned test child did not reach transaction boundary"
        assert child.poll() is None
        child.kill()  # Popen-owned process only: no name matching, PID search or process tree kill.
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    engine = create_database_engine(url)
    try:
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        with Session(engine) as session:
            orphan = session.scalar(select(IngestionRun))
            assert orphan.status == RunStatus.RUNNING  # A killed process cannot finalize its own audit.
            assert orphan.finished_at is None and orphan.records_written == 1
            assert session.get(ConnectorState, orphan.source_id).checkpoint == {"offset": 1}
            assert [r.source_record_id for r in session.scalars(select(IngestedRecord))] == ["0"]
            assert session.scalar(select(AuditEvent)) is None
            orphan_id = orphan.run_id
        spec_data = yaml.safe_load((root / "examples/connectors/rest.yaml").read_text(encoding="utf-8"))
        spec_data["pagination"].update(page_size=1, max_pages=10)
        spec = ConnectorSpec.model_validate(spec_data)
        records = [{"id": str(i), "text": f"fixture {i}"} for i in range(3)]
        options = dict(database_url=url, database_target="sqlite://local/hard-stop.db", initialize_schema=False)
        resumed = asyncio.run(run_connector_durably(FixtureConnector(spec, records), **options))
        assert resumed.inserted == 2 and resumed.records_seen == 2
        replay = asyncio.run(run_connector_durably(FixtureConnector(spec, records), **options))
        assert replay.inserted == replay.updated == 0
        with Session(engine) as session:
            assert session.get(ConnectorState, spec.id).checkpoint == {"offset": 3}
            assert {r.source_record_id for r in session.scalars(select(IngestedRecord))} == {"0", "1", "2"}
            assert session.get(IngestionRun, orphan_id).status == RunStatus.RUNNING
            assert len(session.scalars(select(AuditEvent).where(AuditEvent.action == "connector_run_completed")).all()) == 2
    finally:
        engine.dispose()
