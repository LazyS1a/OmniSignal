from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omnisignal.connectors import FileRawResponseArchive, GitHubIssueSearchConnector
from omnisignal.connectors.github_cli import run_collection
from omnisignal.contracts import CollectRequest, ConnectorFailure, ConnectorSpec, ErrorCategory
from omnisignal.storage import (
    Base,
    ConnectorState,
    IngestedRecord,
    IngestionRun,
    RunStatus,
    SourceControlState,
    create_database_engine,
    mark_records_deleted,
    upsert_records,
)


FIXED_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
QUERY = "repo:github/docs is:issue API"
SPEC_PATH = Path(__file__).parents[1] / "examples" / "connectors" / "github_issue_search.yaml"


def load_spec() -> ConnectorSpec:
    return ConnectorSpec.model_validate(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))


def issue_item(issue_id: int, *, title: str = "API feedback") -> dict[str, object]:
    return {
        "id": issue_id,
        "url": f"https://api.github.com/repos/github/docs/issues/{issue_id}",
        "html_url": f"https://github.com/github/docs/issues/{issue_id}",
        "repository_url": "https://api.github.com/repos/github/docs",
        "number": issue_id,
        "title": title,
        "body": "Public feedback body",
        "state": "open",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "closed_at": None,
        "user": {"login": "must-not-be-persisted", "email": "must-not-be-persisted@example.test"},
    }


def search_document(*items: dict[str, object], incomplete: bool = False) -> dict[str, object]:
    return {"total_count": len(items), "incomplete_results": incomplete, "items": list(items)}


def client_for(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, follow_redirects=False)


def test_reference_github_spec_is_valid_and_minimal() -> None:
    spec = load_spec()
    assert spec.limits.max_concurrency == 1
    assert "user" not in spec.field_allowlist
    assert "email" not in spec.field_allowlist


def test_archive_reuses_identical_content_across_observation_times(tmp_path: Path) -> None:
    archive = FileRawResponseArchive(tmp_path / "archive")
    first = archive.store(
        source_id="github_issue_search",
        request_url="https://api.github.com/search/issues?q=stable",
        status_code=200,
        headers={"ETag": '"stable"', "X-RateLimit-Remaining": "59"},
        body={"items": []},
        collected_at=FIXED_NOW,
    )
    second = archive.store(
        source_id="github_issue_search",
        request_url="https://api.github.com/search/issues?q=stable",
        status_code=200,
        headers={"ETag": '"stable"', "X-RateLimit-Remaining": "58"},
        body={"items": []},
        collected_at=FIXED_NOW.replace(day=4),
    )
    assert first.path == second.path
    assert second.reused is True
    assert len(list((tmp_path / "archive").rglob("*.json.gz"))) == 1


def test_collect_resumes_from_link_checkpoint_and_archives_only_allowed_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            page = request.url.params.get("page")
            assert request.headers["x-github-api-version"] == "2022-11-28"
            assert "authorization" not in request.headers
            if page == "1":
                next_url = str(request.url.copy_set_param("page", "2"))
                return httpx.Response(
                    200,
                    json=search_document(issue_item(101)),
                    headers={"Link": f'<{next_url}>; rel="next"', "ETag": '"page-one"'},
                )
            return httpx.Response(200, json=search_document(issue_item(102)), headers={"ETag": '"page-two"'})

        archive = FileRawResponseArchive(tmp_path / "archive")
        client = client_for(httpx.MockTransport(handler))
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, archive=archive, client=client, now=lambda: FIXED_NOW)
        first = await connector.collect(CollectRequest(run_id="run-page-1", limit=1))
        await connector.close()

        resumed_client = client_for(httpx.MockTransport(handler))
        resumed = GitHubIssueSearchConnector(
            load_spec(), query=QUERY, archive=archive, client=resumed_client, now=lambda: FIXED_NOW
        )
        second = await resumed.collect(
            CollectRequest(run_id="run-page-2", limit=1, checkpoint=first.next_checkpoint)
        )
        await resumed.close()
        await client.aclose()
        await resumed_client.aclose()

        assert [record.source_record_id for record in first.records] == ["101"]
        assert [record.source_record_id for record in second.records] == ["102"]
        assert first.has_more is True
        assert second.has_more is False
        assert "user" not in first.records[0].payload
        assert first.records[0].raw_archive_sha256 is not None
        assert second.records[0].raw_archive_sha256 is not None
        assert len(requests) == 2

        archived = sorted((tmp_path / "archive").rglob("*.json.gz"))
        assert len(archived) == 2
        decoded = gzip.decompress(archived[0].read_bytes()).decode("utf-8")
        assert json.loads(decoded)["body"]["record_links"]
        assert "must-not-be-persisted" not in decoded
        assert "authorization" not in decoded.lower()

    asyncio.run(scenario())


def test_conditional_request_uses_etag_and_304_returns_no_duplicates() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json=search_document(issue_item(201)), headers={"ETag": '"stable"'})
            assert request.headers["if-none-match"] == '"stable"'
            return httpx.Response(304)

        client = client_for(httpx.MockTransport(handler))
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, client=client, now=lambda: FIXED_NOW)
        first = await connector.collect(CollectRequest(run_id="run-etag-1", limit=10))
        second = await connector.collect(
            CollectRequest(run_id="run-etag-2", limit=10, checkpoint=first.next_checkpoint)
        )
        assert len(first.records) == 1
        assert second.records == ()
        assert second.has_more is False
        await connector.close()
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [429, 500])
def test_transient_failures_recover_with_bounded_backoff(status: int) -> None:
    async def scenario() -> None:
        calls = 0
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                headers = {"Retry-After": "1"} if status == 429 else {}
                return httpx.Response(status, json={"message": "rate limit"}, headers=headers)
            return httpx.Response(200, json=search_document(issue_item(301)))

        client = client_for(httpx.MockTransport(handler))
        connector = GitHubIssueSearchConnector(
            load_spec(), query=QUERY, client=client, sleep=fake_sleep, now=lambda: FIXED_NOW
        )
        batch = await connector.collect(CollectRequest(run_id=f"run-retry-{status}", limit=10))
        assert [record.source_record_id for record in batch.records] == ["301"]
        assert sleeps == [1]
        await connector.close()
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "headers", "category"),
    [
        (401, {}, ErrorCategory.AUTHENTICATION),
        (403, {}, ErrorCategory.PERMISSION),
        (429, {"Retry-After": "3600"}, ErrorCategory.RATE_LIMIT),
    ],
)
def test_nonrecoverable_auth_permission_and_long_quota_wait_pause_cleanly(
    status: int, headers: dict[str, str], category: ErrorCategory
) -> None:
    async def scenario() -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = client_for(
            httpx.MockTransport(lambda request: httpx.Response(status, json={"message": "denied"}, headers=headers))
        )
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, client=client, sleep=fake_sleep)
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id=f"run-stop-{status}", limit=10))
        assert raised.value.category == category
        assert sleeps == []
        await connector.close()
        await client.aclose()

    asyncio.run(scenario())


def test_untrusted_pagination_link_cannot_leave_official_endpoint() -> None:
    async def scenario() -> None:
        response = httpx.Response(
            200,
            json=search_document(issue_item(401)),
            headers={"Link": '<https://evil.example/search/issues?page=2>; rel="next"'},
        )
        client = client_for(httpx.MockTransport(lambda request: response))
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, client=client)
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id="run-bad-link", limit=10))
        assert raised.value.category == ErrorCategory.POLICY_VIOLATION
        await connector.close()
        await client.aclose()

    asyncio.run(scenario())


def test_three_replays_upsert_once_then_update_and_tombstone() -> None:
    async def collect(title: str):
        client = client_for(
            httpx.MockTransport(lambda request: httpx.Response(200, json=search_document(issue_item(501, title=title))))
        )
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, client=client, now=lambda: FIXED_NOW)
        batch = await connector.collect(CollectRequest(run_id=f"run-{title}", limit=10))
        await connector.close()
        await client.aclose()
        return batch.records

    records = asyncio.run(collect("original"))
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        summaries = [upsert_records(session, records) for _ in range(3)]
        session.commit()
        assert summaries[0].inserted == 1
        assert [summary.unchanged for summary in summaries[1:]] == [1, 1]
        assert session.scalar(select(func.count()).select_from(IngestedRecord)) == 1

        changed = asyncio.run(collect("updated"))
        summary = upsert_records(session, changed)
        session.commit()
        assert summary.updated == 1
        stored = session.get(IngestedRecord, (load_spec().id, "501"))
        assert stored is not None
        assert stored.payload["title"] == "updated"

        assert mark_records_deleted(session, load_spec().id, ["501"], deleted_at=FIXED_NOW) == 1
        assert mark_records_deleted(session, load_spec().id, ["501"], deleted_at=FIXED_NOW) == 0
        session.commit()
        assert stored.deleted_at is not None
        assert stored.deleted_at.replace(tzinfo=timezone.utc) == FIXED_NOW


def test_incomplete_search_is_degraded_and_never_infers_deletions() -> None:
    async def scenario() -> None:
        client = client_for(
            httpx.MockTransport(
                lambda request: httpx.Response(200, json=search_document(issue_item(601), incomplete=True))
            )
        )
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, client=client, now=lambda: FIXED_NOW)
        batch = await connector.collect(CollectRequest(run_id="run-incomplete", limit=10))
        health = await connector.health()
        deletions = await connector.sync_deletions(batch.next_checkpoint)
        assert health.status.value == "degraded"
        assert health.detail_code == "incomplete_results"
        assert deletions.source_record_ids == ()
        await connector.close()
        await client.aclose()

    asyncio.run(scenario())


def test_deletion_requires_two_safe_complete_snapshots_of_absence() -> None:
    async def scenario() -> None:
        documents = iter(
            [
                search_document(issue_item(701)),
                search_document(),
                search_document(),
            ]
        )
        client = client_for(
            httpx.MockTransport(lambda request: httpx.Response(200, json=next(documents)))
        )
        connector = GitHubIssueSearchConnector(load_spec(), query=QUERY, client=client, now=lambda: FIXED_NOW)
        first = await connector.collect(CollectRequest(run_id="run-delete-baseline", limit=10))
        second = await connector.collect(
            CollectRequest(run_id="run-delete-missing-1", limit=10, checkpoint=first.next_checkpoint)
        )
        second_deletions = await connector.sync_deletions(second.next_checkpoint)
        assert second_deletions.source_record_ids == ()

        third = await connector.collect(
            CollectRequest(
                run_id="run-delete-missing-2",
                limit=10,
                checkpoint=second_deletions.next_checkpoint,
            )
        )
        third_deletions = await connector.sync_deletions(third.next_checkpoint)
        assert third_deletions.source_record_ids == ("701",)
        assert third_deletions.next_checkpoint is not None
        assert third_deletions.next_checkpoint.value["pending_deletions"] == []
        await connector.close()
        await client.aclose()

    asyncio.run(scenario())


def test_durable_runner_commits_records_and_checkpoint_together(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "runner.db"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        client = client_for(
            httpx.MockTransport(lambda request: httpx.Response(200, json=search_document(issue_item(801))))
        )

        async def no_sleep(seconds: float) -> None:
            raise AssertionError(f"single page run should not sleep: {seconds}")

        summary = await run_collection(
            spec=load_spec(),
            query=QUERY,
            database_url=database_url,
            database_target="sqlite+pysqlite://local/runner.db",
            archive_root=tmp_path / "archive",
            initialize_schema=True,
            client=client,
            sleep=no_sleep,
        )
        await client.aclose()
        assert summary.inserted == 1
        assert summary.cycle_complete is True
        assert summary.cycle_safe_snapshot is True

        engine = create_database_engine(database_url)
        with Session(engine) as session:
            assert session.get(IngestedRecord, (load_spec().id, "801")) is not None
            state = session.get(ConnectorState, load_spec().id)
            assert state is not None
            assert state.checkpoint["cycle_complete"] is True
            run = session.get(IngestionRun, summary.run_id)
            assert run is not None
            assert run.status == RunStatus.SUCCEEDED
        engine.dispose()

    asyncio.run(scenario())


def test_github_runner_refuses_disabled_source_before_http_or_run_row(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "disabled-github.db"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        engine = create_database_engine(database_url)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                SourceControlState(
                    source_id=load_spec().id,
                    enabled=False,
                    version=1,
                    updated_by="ops-one",
                    reason="planned maintenance",
                )
            )
            session.commit()
        engine.dispose()

        def unexpected_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"disabled source made HTTP request: {request.url}")

        client = client_for(httpx.MockTransport(unexpected_request))
        with pytest.raises(ConnectorFailure) as raised:
            await run_collection(
                spec=load_spec(),
                query=QUERY,
                database_url=database_url,
                database_target="sqlite+pysqlite://local/disabled-github.db",
                archive_root=tmp_path / "archive",
                initialize_schema=False,
                client=client,
            )
        assert raised.value.category == ErrorCategory.POLICY_VIOLATION
        await client.aclose()

        engine = create_database_engine(database_url)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(IngestionRun)) == 0
        engine.dispose()

    asyncio.run(scenario())


def test_durable_runner_marks_exhausted_quota_as_paused(tmp_path: Path) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "quota.db"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        client = client_for(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    429,
                    json={"message": "rate limit exceeded"},
                    headers={"Retry-After": "3600"},
                )
            )
        )
        with pytest.raises(ConnectorFailure) as raised:
            await run_collection(
                spec=load_spec(),
                query=QUERY,
                database_url=database_url,
                database_target="sqlite+pysqlite://local/quota.db",
                archive_root=tmp_path / "archive",
                initialize_schema=True,
                client=client,
            )
        assert raised.value.category == ErrorCategory.RATE_LIMIT
        await client.aclose()

        engine = create_database_engine(database_url)
        with Session(engine) as session:
            runs = list(session.scalars(select(IngestionRun)))
            assert len(runs) == 1
            assert runs[0].status == RunStatus.PAUSED
            assert runs[0].error_code == ErrorCategory.RATE_LIMIT.value
        engine.dispose()

    asyncio.run(scenario())
