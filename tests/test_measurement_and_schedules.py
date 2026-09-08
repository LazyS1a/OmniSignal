from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from omnisignal.collection_jobs import CollectionTaskCatalog
from omnisignal.snapshot_schedules import SnapshotScheduleCatalog, SnapshotSchedulerService
from omnisignal.storage import Base, CollectionJob, create_database_engine


ROOT = Path(__file__).parents[1]


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)


def test_collection_catalog_binds_versioned_measurement_definitions() -> None:
    catalog = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    public = catalog.tasks["public_search_signals_example"]
    searxng = catalog.tasks["searxng_results_example"]
    youtube = catalog.tasks["youtube_visibility_example"]

    public_context = catalog.observation_context(public)
    searxng_context = catalog.observation_context(searxng)
    youtube_context = catalog.observation_context(youtube)

    assert public_context.keyword_set.model_dump() == {"id": "example_ai_assistants", "version": 1}
    assert public_context.entity_set is None
    assert youtube_context.entity_set is not None
    assert youtube_context.entity_set.id == "example_note_tools"
    assert public_context.definition_hash != youtube_context.definition_hash
    assert searxng_context.keyword_set.model_dump() == {"id": "example_ai_assistants", "version": 1}
    assert searxng_context.entity_set is None
    assert searxng_context.definition_hash != public_context.definition_hash


def test_collection_catalog_rejects_keyword_policy_drift(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        f"""version: 1
tasks:
  - id: drift_task
    display_name: Drift task
    description: Deliberate keyword policy drift fixture.
    connector: public_search_signals
    source_id: public_search_signals
    policy: tests/fixtures/public_search_policy_drift.yaml
    keyword_set: {{id: example_ai_assistants, version: 1}}
    entity_set: null
    access_tier: anonymous_public
    observation_scope: Deliberate policy drift observation fixture.
    is_example: true
    enabled: true
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="keyword set differs"):
        CollectionTaskCatalog(tasks, ROOT)


def test_schedule_engine_is_double_gated_and_never_due_in_production_config(tmp_path: Path) -> None:
    production = SnapshotScheduleCatalog(
        ROOT / "config" / "snapshot_schedules.yaml",
        task_ids={"public_search_signals_example", "searxng_results_example", "youtube_visibility_example"},
    )
    now = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    assert production.auto_start is False
    assert production.due_schedule_ids(now=now, last_triggered_at={}) == ()

    active_path = tmp_path / "active.yaml"
    active_path.write_text(
        """version: 1
auto_start: true
schedules:
  - id: hourly_fixture
    display_name: Hourly fixture
    task_id: fixture_task
    status: active
    interval_minutes: 60
    anchor_utc: "2026-09-08T00:00:00Z"
""",
        encoding="utf-8",
    )
    active = SnapshotScheduleCatalog(active_path, task_ids={"fixture_task"})
    assert active.due_schedule_ids(now=now, last_triggered_at={}) == ("hourly_fixture",)
    last = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    assert active.due_schedule_ids(now=now, last_triggered_at={"hourly_fixture": last}) == ()


def test_scheduler_service_can_queue_one_idempotent_slot_but_production_gates_are_closed(tmp_path: Path) -> None:
    tasks = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    disabled_catalog = SnapshotScheduleCatalog(
        ROOT / "config" / "snapshot_schedules.yaml",
        task_ids=set(tasks.tasks),
    )
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'scheduler.db').as_posix()}")
    Base.metadata.create_all(engine)
    executor = FakeExecutor()
    disabled = SnapshotSchedulerService(
        engine=engine,
        schedule_catalog=disabled_catalog,
        collection_catalog=tasks,
        collection_executor=executor,
        registry_path=ROOT / "governance" / "source_registry.yaml",
        project_root=ROOT,
        runtime_enabled=False,
    )
    assert disabled.start() is False
    assert disabled.tick(now=datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)) == ()
    assert executor.submitted == []

    active_path = tmp_path / "active-service.yaml"
    active_path.write_text(
        """version: 1
auto_start: true
schedules:
  - id: public_fixture
    display_name: Public fixture
    task_id: public_search_signals_example
    status: active
    interval_minutes: 60
    anchor_utc: "2026-09-08T00:00:00Z"
""",
        encoding="utf-8",
    )
    active_catalog = SnapshotScheduleCatalog(active_path, task_ids=set(tasks.tasks))
    active = SnapshotSchedulerService(
        engine=engine,
        schedule_catalog=active_catalog,
        collection_catalog=tasks,
        collection_executor=executor,
        registry_path=ROOT / "governance" / "source_registry.yaml",
        project_root=ROOT,
        runtime_enabled=True,
    )
    now = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    queued = active.tick(now=now)
    assert len(queued) == 1 and executor.submitted == [queued[0]]
    assert active.tick(now=now) == ()
    with Session(engine) as session:
        job = session.get(CollectionJob, queued[0])
        assert job is not None
        assert job.actor == "snapshot-scheduler:public_fixture"
        assert job.status == "pending"
    engine.dispose()
