"""One-shot durable runner for the official GitHub issue search connector."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from omnisignal.config import Settings
from omnisignal.contracts import (
    AuthKind,
    AuthSpec,
    Checkpoint,
    CollectRequest,
    ConnectorFailure,
    ConnectorSpec,
    ErrorCategory,
)
from omnisignal.storage import (
    AuditEvent,
    Base,
    ConnectorState,
    IngestionRun,
    RunStatus,
    assert_source_enabled,
    create_database_engine,
    mark_records_deleted,
    upsert_records,
)

from .archive import FileRawResponseArchive
from .github_issues import GitHubIssueSearchConnector


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SPEC = PROJECT_ROOT / "examples" / "connectors" / "github_issue_search.yaml"
DEFAULT_ARCHIVE = PROJECT_ROOT / "data" / "raw" / "github_issue_search"
DEFAULT_SQLITE = PROJECT_ROOT / "data" / "omnisignal.db"


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    source_id: str
    pages: int
    records_seen: int
    inserted: int
    updated: int
    unchanged: int
    tombstoned: int
    cycle_complete: bool
    cycle_safe_snapshot: bool
    database_target: str
    archive_root: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_spec(path: Path, token_env: str | None) -> ConnectorSpec:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = ConnectorSpec.model_validate(document)
    if token_env is None:
        return spec
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", token_env) is None:
        raise ValueError("token environment variable name must be uppercase letters, digits or underscores")
    return spec.model_copy(update={"auth": AuthSpec(kind=AuthKind.BEARER, secret_ref=f"env/{token_env}")})


def _database_configuration() -> tuple[str, str, bool]:
    if os.getenv("OMNISIGNAL_DATABASE_URL") or os.getenv("OMNISIGNAL_DB_PASSWORD"):
        settings = Settings.from_env()
        return settings.database_url, settings.safe_database_target(), False
    DEFAULT_SQLITE.parent.mkdir(parents=True, exist_ok=True)
    database_url = URL.create("sqlite+pysqlite", database=str(DEFAULT_SQLITE)).render_as_string(hide_password=False)
    safe_target = f"sqlite+pysqlite://local/{DEFAULT_SQLITE.name}"
    return database_url, safe_target, True


def _load_checkpoint(session: Session, source_id: str) -> Checkpoint | None:
    state = session.get(ConnectorState, source_id)
    if state is None:
        return None
    return Checkpoint(
        source_id=source_id,
        value=state.checkpoint,
        version=state.checkpoint_version,
        updated_at=state.updated_at,
    )


def _save_checkpoint(session: Session, checkpoint: Checkpoint) -> None:
    state = session.get(ConnectorState, checkpoint.source_id)
    if state is None:
        session.add(
            ConnectorState(
                source_id=checkpoint.source_id,
                checkpoint=checkpoint.value,
                checkpoint_version=checkpoint.version,
                updated_at=checkpoint.updated_at,
            )
        )
        return
    state.checkpoint = checkpoint.value
    state.checkpoint_version = checkpoint.version
    state.updated_at = checkpoint.updated_at


async def run_collection(
    *,
    spec: ConnectorSpec,
    query: str,
    database_url: str,
    database_target: str,
    archive_root: Path,
    initialize_schema: bool,
    client: httpx.AsyncClient | None = None,
    sleep: Any = asyncio.sleep,
) -> RunSummary:
    engine = create_database_engine(database_url)
    if initialize_schema:
        Base.metadata.create_all(engine)
    try:
        with Session(engine) as control_session:
            assert_source_enabled(control_session, spec.id)
    except Exception:
        engine.dispose()
        raise

    run_id = str(uuid4())
    archive = FileRawResponseArchive(archive_root)
    connector = GitHubIssueSearchConnector(spec, query=query, archive=archive, client=client, sleep=sleep)
    pages = 0
    seen = inserted = updated = unchanged = tombstoned = 0
    checkpoint: Checkpoint | None = None

    with Session(engine) as session:
        run = IngestionRun(
            run_id=run_id,
            source_id=spec.id,
            idempotency_key=run_id,
            status=RunStatus.RUNNING,
            started_at=_utc_now(),
        )
        session.add(run)
        session.commit()
        checkpoint = _load_checkpoint(session, spec.id)

        try:
            while pages < spec.pagination.max_pages:
                batch = await connector.collect(
                    CollectRequest(
                        run_id=run_id,
                        limit=spec.pagination.page_size,
                        checkpoint=checkpoint,
                    )
                )
                upsert = upsert_records(session, batch.records)
                deletion_batch = await connector.sync_deletions(batch.next_checkpoint)
                effective_checkpoint = deletion_batch.next_checkpoint or batch.next_checkpoint
                deleted = mark_records_deleted(session, spec.id, deletion_batch.source_record_ids)
                _save_checkpoint(session, effective_checkpoint)

                pages += 1
                seen += len(batch.records)
                inserted += upsert.inserted
                updated += upsert.updated
                unchanged += upsert.unchanged
                tombstoned += deleted
                run.records_seen = seen
                run.records_written = inserted + updated + tombstoned
                session.commit()
                checkpoint = effective_checkpoint

                if not batch.has_more:
                    break
                await sleep(60 / spec.limits.requests_per_minute)

            run.status = RunStatus.SUCCEEDED
            run.finished_at = _utc_now()
            session.add(
                AuditEvent(
                    run_id=run_id,
                    actor="github_cli",
                    action="connector_run_completed",
                    target=spec.id,
                    detail={
                        "pages": pages,
                        "records_seen": seen,
                        "records_written": run.records_written,
                        "tombstoned": tombstoned,
                    },
                )
            )
            session.commit()
        except ConnectorFailure as exc:
            session.rollback()
            failed_run = session.get(IngestionRun, run_id)
            if failed_run is not None:
                should_pause = exc.category in {
                    ErrorCategory.AUTHENTICATION,
                    ErrorCategory.PERMISSION,
                    ErrorCategory.RATE_LIMIT,
                    ErrorCategory.RESOURCE_EXHAUSTED,
                }
                failed_run.status = RunStatus.PAUSED if should_pause else RunStatus.FAILED
                failed_run.error_code = exc.category.value
                failed_run.error_summary = str(exc)[:500]
                failed_run.finished_at = _utc_now()
                session.add(
                    AuditEvent(
                        run_id=run_id,
                        actor="github_cli",
                        action="connector_run_stopped",
                        target=spec.id,
                        detail={
                            "category": exc.category.value,
                            "action": "pause" if should_pause else exc.action.value,
                            "retry_after_seconds": exc.retry_after_seconds,
                        },
                    )
                )
                session.commit()
            raise
        finally:
            await connector.close()
            engine.dispose()

    checkpoint_value = checkpoint.value if checkpoint is not None else {}
    return RunSummary(
        run_id=run_id,
        source_id=spec.id,
        pages=pages,
        records_seen=seen,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        tombstoned=tombstoned,
        cycle_complete=bool(checkpoint_value.get("cycle_complete", False)),
        cycle_safe_snapshot=bool(checkpoint_value.get("cycle_safe_snapshot", False)),
        database_target=database_target,
        archive_root=str(archive_root.resolve()),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect public GitHub Issue/PR search results durably")
    parser.add_argument("--query", required=True, help="GitHub issue search query")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--token-env", help="Name of an environment variable containing a GitHub token")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        spec = _load_spec(args.spec.resolve(), args.token_env)
        database_url, database_target, initialize_schema = _database_configuration()
        summary = asyncio.run(
            run_collection(
                spec=spec,
                query=args.query,
                database_url=database_url,
                database_target=database_target,
                archive_root=args.archive_root.resolve(),
                initialize_schema=initialize_schema,
            )
        )
    except ConnectorFailure as exc:
        print(
            json.dumps(
                {
                    "status": "stopped_safely",
                    "category": exc.category.value,
                    "action": exc.action.value,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "invalid_configuration", "message": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({"status": "completed", **asdict(summary)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
