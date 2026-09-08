from __future__ import annotations

import asyncio
import gzip
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omnisignal.connectors import FileRawResponseArchive, StaticWebConnector, StaticWebPolicy
from omnisignal.connectors.durable import run_connector_durably
from omnisignal.contracts import CollectRequest, ConnectorFailure, ConnectorSpec, ErrorCategory, RuntimeLimits
from omnisignal.governance import SourceApproval, load_source_approval
from omnisignal.storage import IngestedRecord, IngestionRun, RunStatus, create_database_engine


SPEC_PATH = Path(__file__).parents[1] / "examples" / "connectors" / "static_html.yaml"


class FixtureHandler(BaseHTTPRequestHandler):
    mode = "ok"
    page_requests = 0
    conditional_requests = 0
    etag = '"fixture-v1"'

    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            body = (
                "User-agent: *\nDisallow: /articles/\n"
                if type(self).mode == "robots_forbid"
                else "User-agent: *\nAllow: /\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/articles/one":
            self.send_response(404)
            self.end_headers()
            return

        type(self).page_requests += 1
        if type(self).mode == "missing":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == type(self).etag:
            type(self).conditional_requests += 1
            self.send_response(304)
            self.send_header("ETag", type(self).etag)
            self.end_headers()
            return

        if type(self).mode == "captcha":
            html = "<html><body><div class='g-recaptcha'>verify you are human</div></body></html>"
        elif type(self).mode == "drift":
            html = "<html><body><main><h1>Changed shell</h1><p>No article element remains.</p></main></body></html>"
        else:
            html = """
            <html><head><title>Fixture article</title><script>secretScript()</script></head>
            <body><article data-user="discard-me"><h1>Fixture article</h1>
            <p>This is project-owned public fixture text used to validate the compliant crawler.</p>
            <p>It is deliberately long enough for deterministic Trafilatura extraction and contract testing.</p>
            <form><input name="session_cookie" value="discard-me"></form>
            <time datetime="2026-09-03">September 3</time></article></body></html>
            """
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("ETag", type(self).etag)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def fixture_server(mode: str = "ok") -> Iterator[str]:
    FixtureHandler.mode = mode
    FixtureHandler.page_requests = 0
    FixtureHandler.conditional_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_connector(base_url: str, tmp_path: Path) -> StaticWebConnector:
    data = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    prefix = f"{base_url}/articles/"
    data["allowed_targets"] = [prefix]
    data["limits"] = RuntimeLimits(
        timeout_seconds=5,
        max_attempts=2,
        requests_per_minute=60_000,
        max_concurrency=1,
        max_records_per_run=10,
    ).model_dump(mode="json")
    spec = ConnectorSpec.model_validate(data)
    policy = StaticWebPolicy(
        start_urls=(f"{base_url}/articles/one",),
        allowed_url_prefixes=(prefix,),
        required_selectors=("article", "article h1"),
        min_text_chars=80,
        max_response_bytes=100_000,
        allow_private_network=True,
    )
    approval = SourceApproval(
        source_id=spec.id,
        permission="registry:fixture_static_html:v1:test",
        authorization_basis="project owned fixture",
        rate_budget_rpm=60_000,
    )
    return StaticWebConnector(
        spec,
        policy,
        approval,
        workspace=tmp_path / "worker",
        archive=FileRawResponseArchive(tmp_path / "archive"),
    )


def test_policy_rejects_prefix_confusion_and_private_network_by_default() -> None:
    with pytest.raises(ValueError, match="outside allowed prefixes"):
        StaticWebPolicy(
            start_urls=("https://example.test.evil/articles/one",),
            allowed_url_prefixes=("https://example.test/articles/",),
        )
    with pytest.raises(ValueError, match="public web sources must use https"):
        StaticWebPolicy(
            start_urls=("http://127.0.0.1/articles/one",),
            allowed_url_prefixes=("http://127.0.0.1/articles/",),
        )


def test_source_registry_approves_only_the_registered_scope(tmp_path: Path) -> None:
    data = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    spec = ConnectorSpec.model_validate(data)
    registry_path = Path(__file__).parents[1] / "governance" / "source_registry.yaml"
    approval = load_source_approval(registry_path, spec)
    assert approval.source_id == spec.id
    assert approval.permission.startswith("registry:fixture_static_html:v1:")

    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    source = next(item for item in registry["sources"] if item["id"] == spec.id)
    source["status"] = "pending"
    rejected_path = tmp_path / "source_registry.yaml"
    rejected_path.write_text(yaml.safe_dump(registry, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConnectorFailure) as raised:
        load_source_approval(rejected_path, spec)
    assert raised.value.category == ErrorCategory.POLICY_VIOLATION


@pytest.mark.parametrize("mutation", ["rate", "target", "field"])
def test_source_registry_rejects_scope_expansion(mutation: str) -> None:
    data = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    if mutation == "rate":
        data["limits"]["requests_per_minute"] = 13
    elif mutation == "target":
        data["allowed_targets"] = ["https://unregistered.example/articles/"]
    else:
        data["field_allowlist"].append("engagement")
    spec = ConnectorSpec.model_validate(data)
    registry_path = Path(__file__).parents[1] / "governance" / "source_registry.yaml"
    with pytest.raises(ConnectorFailure) as raised:
        load_source_approval(registry_path, spec)
    assert raised.value.category == ErrorCategory.POLICY_VIOLATION


def test_real_scrapy_worker_extracts_caches_and_archives_sanitized_html(tmp_path: Path) -> None:
    async def scenario(base_url: str) -> None:
        connector = make_connector(base_url, tmp_path)
        first = await connector.collect(CollectRequest(run_id="web-first", limit=1))
        second = await connector.collect(
            CollectRequest(run_id="web-second", limit=1, checkpoint=first.next_checkpoint)
        )
        assert len(first.records) == 1
        assert len(second.records) == 1
        assert first.records[0].source_record_id == second.records[0].source_record_id
        assert first.records[0].raw_hash == second.records[0].raw_hash
        assert first.records[0].raw_archive_sha256 is not None
        assert second.records[0].raw_archive_sha256 is not None
        assert first.records[0].payload["quality_status"] == "accepted"
        assert "project-owned public fixture text" in first.records[0].payload["text"]
        assert FixtureHandler.conditional_requests >= 1
        await connector.close()

    with fixture_server() as base_url:
        asyncio.run(scenario(base_url))

    archives = list((tmp_path / "archive").rglob("*.json.gz"))
    assert len(archives) == 1
    document = json.loads(gzip.decompress(archives[0].read_bytes()))
    sanitized = document["body"]["sanitized_html"]
    assert document["body"]["record_links"]
    assert "secretScript" not in sanitized
    assert "session_cookie" not in sanitized
    assert "discard-me" not in sanitized
    assert "datetime=\"2026-09-03\"" in sanitized


@pytest.mark.parametrize(
    ("mode", "category"),
    [
        ("robots_forbid", ErrorCategory.POLICY_VIOLATION),
        ("captcha", ErrorCategory.PERMISSION),
        ("drift", ErrorCategory.SCHEMA_DRIFT),
    ],
)
def test_barriers_and_structure_drift_stop_instead_of_bypass(
    mode: str, category: ErrorCategory, tmp_path: Path
) -> None:
    async def scenario(base_url: str) -> None:
        connector = make_connector(base_url, tmp_path)
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id=f"web-{mode}", limit=1))
        assert raised.value.category == category
        await connector.close()

    with fixture_server(mode) as base_url:
        asyncio.run(scenario(base_url))


def test_404_requires_two_observations_before_tombstone(tmp_path: Path) -> None:
    async def scenario(base_url: str) -> None:
        connector = make_connector(base_url, tmp_path)
        first = await connector.collect(CollectRequest(run_id="web-missing-one", limit=1))
        first_deletions = await connector.sync_deletions(first.next_checkpoint)
        assert first_deletions.source_record_ids == ()
        second = await connector.collect(
            CollectRequest(
                run_id="web-missing-two",
                limit=1,
                checkpoint=first_deletions.next_checkpoint,
            )
        )
        second_deletions = await connector.sync_deletions(second.next_checkpoint)
        assert len(second_deletions.source_record_ids) == 1
        await connector.close()

    with fixture_server("missing") as base_url:
        asyncio.run(scenario(base_url))


def test_durable_static_web_run_is_idempotent(tmp_path: Path) -> None:
    async def scenario(base_url: str) -> None:
        database_path = tmp_path / "web.db"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        first = await run_connector_durably(
            make_connector(base_url, tmp_path),
            database_url=database_url,
            database_target="sqlite+pysqlite://local/web.db",
            initialize_schema=True,
        )
        second = await run_connector_durably(
            make_connector(base_url, tmp_path),
            database_url=database_url,
            database_target="sqlite+pysqlite://local/web.db",
            initialize_schema=True,
        )
        assert first.inserted == 1
        assert second.inserted == 0
        assert second.unchanged == 1

        engine = create_database_engine(database_url)
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(IngestedRecord)) == 1
            runs = list(session.scalars(select(IngestionRun).order_by(IngestionRun.created_at)))
            assert [run.status for run in runs] == [RunStatus.SUCCEEDED, RunStatus.SUCCEEDED]
        engine.dispose()

    with fixture_server("ok") as base_url:
        asyncio.run(scenario(base_url))


def test_durable_static_web_run_records_robots_pause(tmp_path: Path) -> None:
    async def scenario(base_url: str) -> None:
        database_path = tmp_path / "robots.db"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        with pytest.raises(ConnectorFailure) as raised:
            await run_connector_durably(
                make_connector(base_url, tmp_path),
                database_url=database_url,
                database_target="sqlite+pysqlite://local/robots.db",
                initialize_schema=True,
            )
        assert raised.value.category == ErrorCategory.POLICY_VIOLATION
        engine = create_database_engine(database_url)
        with Session(engine) as session:
            run = session.scalar(select(IngestionRun))
            assert run is not None
            assert run.status == RunStatus.PAUSED
            assert run.error_code == ErrorCategory.POLICY_VIOLATION.value
        engine.dispose()

    with fixture_server("robots_forbid") as base_url:
        asyncio.run(scenario(base_url))
