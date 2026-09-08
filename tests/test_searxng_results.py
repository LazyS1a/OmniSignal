from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
import yaml

from omnisignal.connectors import FileRawResponseArchive
from omnisignal.connectors.durable import run_connector_durably
from omnisignal.connectors.searxng_policy import SearXNGPolicy
from omnisignal.connectors.searxng_protocol import OUTPUT_SCHEMA_FINGERPRINT, PROTOCOL_VERSION
from omnisignal.connectors.searxng_results import SearXNGResultsConnector
from omnisignal.collection_jobs import CollectionExecutor, CollectionTaskCatalog
from omnisignal.config import Environment, Settings
from omnisignal.contracts import CollectRequest, ConnectorFailure, ConnectorSpec, ErrorCategory
from omnisignal.governance import load_source_approval
from omnisignal.storage import Base, SourceControlState, create_database_engine


ROOT = Path(__file__).parents[1]
FIXED_NOW = datetime(2026, 9, 6, 12, 30, tzinfo=timezone.utc)


def load_spec() -> ConnectorSpec:
    return ConnectorSpec.model_validate(
        yaml.safe_load((ROOT / "examples/connectors/searxng_results.yaml").read_text(encoding="utf-8"))
    )


def load_policy(**overrides: object) -> SearXNGPolicy:
    document = yaml.safe_load((ROOT / "examples/policies/searxng_results.yaml").read_text(encoding="utf-8"))
    document.update(overrides)
    return SearXNGPolicy.model_validate(document)


def test_local_deployment_keeps_json_loopback_and_bounded_slow_network_budget() -> None:
    settings = yaml.safe_load((ROOT / "deploy/searxng/settings.yml").read_text(encoding="utf-8"))
    compose = yaml.safe_load((ROOT / "compose.searxng.yaml").read_text(encoding="utf-8"))
    policy = yaml.safe_load((ROOT / "examples/policies/searxng_results.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["searxng"]

    assert settings["search"]["formats"] == ["html", "json"]
    assert settings["server"]["limiter"] is False
    assert settings["outgoing"] == {"request_timeout": 12.0, "max_request_timeout": 15.0}
    assert policy["request_timeout_seconds"] > settings["outgoing"]["max_request_timeout"]
    assert service["ports"] == ["127.0.0.1:8888:8080"]
    assert service["image"] == "docker.io/searxng/searxng:2026.9.5-c7f3080aa"
    assert (ROOT / "scripts/status-searxng.ps1").is_file()


def approval(spec: ConnectorSpec | None = None):
    return load_source_approval(ROOT / "governance/source_registry.yaml", spec or load_spec())


def worker_job(policy: SearXNGPolicy, *, limit: int = 200) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "endpoint": policy.endpoint,
        "queries": list(policy.queries),
        "engines": list(policy.engines),
        "categories": list(policy.categories),
        "language": policy.language,
        "time_range": policy.time_range,
        "safe_search": policy.safe_search,
        "max_pages": policy.max_pages,
        "max_results_per_slice": policy.max_results_per_slice,
        "request_timeout_seconds": policy.request_timeout_seconds,
        "limit": limit,
    }


def result_record(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_record_id": "a" * 64,
        "query": "ChatGPT",
        "engine": "duckduckgo",
        "category": "general",
        "position": 1,
        "url": "https://example.test/chatgpt",
        "title": "ChatGPT result",
        "text": "Public result snippet",
        "published_at": None,
        "language": "en-US",
        "time_range": None,
        "observed_at": FIXED_NOW.isoformat(),
        "scope": "searxng_engine_result_snapshot",
        "source_url": "http://127.0.0.1:8888/search",
        "collector_version": "fixture/1",
        "sample_complete": True,
        "quality_status": "accepted",
    }
    value.update(overrides)
    return value


def worker_result(*records: dict[str, object], warnings: list[str] | None = None) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
        "status": "ok",
        "records": list(records),
        "warnings": warnings or [],
        "fetched_at": FIXED_NOW.isoformat(),
        "error": None,
    }


def test_policy_is_loopback_only_and_bounded() -> None:
    assert load_policy().maximum_records == 20
    with pytest.raises(ValueError):
        load_policy(endpoint="https://public.example/search")
    with pytest.raises(ValueError):
        load_policy(endpoint="http://127.0.0.1:9999/search")
    with pytest.raises(ValueError):
        SearXNGPolicy(
            queries=tuple(f"q{i}" for i in range(7)),
            engines=tuple(f"engine{i}" for i in range(7)),
            max_pages=1,
        )


def test_worker_keeps_engine_local_order_and_deduplicates_pages() -> None:
    from omnisignal.connectors import searxng_worker

    policy = load_policy(queries=["ChatGPT"], max_pages=2, max_results_per_slice=3)
    calls: list[dict[str, object]] = []

    def request_page(endpoint: str, params: dict[str, object], timeout: int) -> dict[str, object]:
        assert endpoint == policy.endpoint and timeout == 20
        calls.append(params)
        if params["pageno"] == 1:
            return {
                "results": [
                    {"url": "https://a.test/", "title": "A", "content": "one", "engine": "duckduckgo"},
                    {"url": "https://b.test/", "title": "B", "content": "two", "engines": ["duckduckgo"]},
                ]
            }
        return {
            "results": [
                {"url": "https://b.test/", "title": "B duplicate", "engine": "duckduckgo"},
                {"url": "https://c.test/", "title": "C", "engine": "duckduckgo"},
            ]
        }

    result = searxng_worker.collect(worker_job(policy), now=FIXED_NOW, request_page=request_page)
    assert result["status"] == "ok" and result["warnings"] == []
    assert [item["position"] for item in result["records"]] == [1, 2, 3]
    assert [item["url"] for item in result["records"]] == [
        "https://a.test/",
        "https://b.test/",
        "https://c.test/",
    ]
    assert all(item["sample_complete"] is True for item in result["records"])
    assert calls[0] == {
        "q": "ChatGPT",
        "format": "json",
        "engines": "duckduckgo",
        "language": "en-US",
        "pageno": 1,
        "safesearch": 1,
    }


def test_worker_marks_partial_slice_without_copying_error_text() -> None:
    from omnisignal.connectors import searxng_worker

    policy = load_policy(queries=["ChatGPT"], max_pages=2, max_results_per_slice=5)

    def request_page(endpoint: str, params: dict[str, object], timeout: int) -> dict[str, object]:
        del endpoint, timeout
        if params["pageno"] == 1:
            return {"results": [{"url": "https://a.test/", "title": "A", "engine": "duckduckgo"}]}
        raise RuntimeError("private upstream diagnostic must not escape")

    result = searxng_worker.collect(worker_job(policy), now=FIXED_NOW, request_page=request_page)
    assert result["status"] == "ok" and result["warnings"] == ["slice_unavailable"]
    assert result["records"][0]["sample_complete"] is False
    assert result["records"][0]["quality_status"] == "incomplete"
    assert "private upstream" not in str(result)


def test_worker_maps_rate_limit_and_schema_drift() -> None:
    from omnisignal.connectors import searxng_worker

    policy = load_policy(queries=["ChatGPT"])

    class RateLimited(RuntimeError):
        response = SimpleNamespace(status_code=429)

    limited = searxng_worker.collect(
        worker_job(policy),
        now=FIXED_NOW,
        request_page=lambda endpoint, params, timeout: (_ for _ in ()).throw(RateLimited()),
    )
    drifted = searxng_worker.collect(
        worker_job(policy),
        now=FIXED_NOW,
        request_page=lambda endpoint, params, timeout: {"unexpected": []},
    )
    assert limited["status"] == "error" and limited["error"] == {"kind": "rate_limit", "status": 429}
    assert drifted["status"] == "error" and drifted["error"] == {"kind": "schema_drift", "status": None}


def test_connector_archives_replays_and_preserves_sample_semantics(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def fake_worker(**kwargs: object) -> dict[str, object]:
            assert kwargs["document"]["endpoint"] == "http://127.0.0.1:8888/search"  # type: ignore[index]
            return worker_result(result_record())

        url = URL.create("sqlite+pysqlite", database=str(tmp_path / "searxng.db")).render_as_string()

        async def run():
            connector = SearXNGResultsConnector(
                load_spec(),
                load_policy(),
                approval(),
                workspace=tmp_path / "worker",
                archive=FileRawResponseArchive(tmp_path / "raw"),
                worker_runner=fake_worker,
            )
            return await run_connector_durably(
                connector,
                database_url=url,
                database_target="test",
                initialize_schema=True,
            )

        first, second = await run(), await run()
        assert first.inserted == 1
        assert second.inserted == 0 and second.unchanged == 1
        assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("position", 0),
        ("engine", "google"),
        ("url", "file:///private"),
        ("scope", "search_volume"),
        ("sample_complete", "yes"),
        ("quality_status", "accepted"),
    ],
)
def test_connector_rejects_mislabeled_or_unsafe_records(tmp_path: Path, field: str, value: object) -> None:
    record = result_record()
    if field == "quality_status":
        record["sample_complete"] = False
    record[field] = value

    async def scenario() -> None:
        async def fake_worker(**kwargs: object) -> dict[str, object]:
            return worker_result(record)

        connector = SearXNGResultsConnector(
            load_spec(), load_policy(), approval(), workspace=tmp_path, worker_runner=fake_worker
        )
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id="run-invalid-record", limit=200))
        assert raised.value.category == ErrorCategory.SCHEMA_DRIFT

    asyncio.run(scenario())


def test_connector_maps_worker_rate_limit_without_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = 0

        async def fake_worker(**kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "protocol_version": PROTOCOL_VERSION,
                "schema_fingerprint": OUTPUT_SCHEMA_FINGERPRINT,
                "status": "error",
                "records": [],
                "warnings": ["slice_unavailable"],
                "fetched_at": FIXED_NOW.isoformat(),
                "error": {"kind": "rate_limit", "status": 429},
            }

        connector = SearXNGResultsConnector(
            load_spec(), load_policy(), approval(), workspace=tmp_path, worker_runner=fake_worker
        )
        with pytest.raises(ConnectorFailure) as raised:
            await connector.collect(CollectRequest(run_id="run-rate-limit", limit=200))
        assert raised.value.category == ErrorCategory.RATE_LIMIT
        assert raised.value.retry_after_seconds == 3600 and calls == 1

    asyncio.run(scenario())


def test_disabled_source_never_starts_worker(tmp_path: Path) -> None:
    url = URL.create("sqlite+pysqlite", database=str(tmp_path / "disabled.db")).render_as_string()
    engine = create_database_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            SourceControlState(
                source_id="searxng_results",
                enabled=False,
                version=1,
                updated_by="test",
                reason="test",
            )
        )
        session.commit()
    engine.dispose()

    async def scenario() -> None:
        async def forbidden_worker(**kwargs: object) -> dict[str, object]:
            pytest.fail("disabled source invoked SearXNG worker")

        connector = SearXNGResultsConnector(
            load_spec(), load_policy(), approval(), workspace=tmp_path, worker_runner=forbidden_worker
        )
        with pytest.raises(ConnectorFailure):
            await run_connector_durably(
                connector,
                database_url=url,
                database_target="test",
                initialize_schema=False,
            )

    asyncio.run(scenario())


def test_collection_executor_builds_only_the_allowlisted_searxng_connector(tmp_path: Path) -> None:
    database_url = URL.create("sqlite+pysqlite", database=str(tmp_path / "jobs.db")).render_as_string()
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    catalog = CollectionTaskCatalog(ROOT / "config/collection_tasks.yaml", ROOT)
    executor = CollectionExecutor(
        engine=engine,
        settings=Settings(environment=Environment.TEST, database_url=database_url),
        catalog=catalog,
        registry_path=ROOT / "governance/source_registry.yaml",
        project_root=ROOT,
    )
    try:
        task = catalog.tasks["searxng_results_example"]
        connector = executor._build_connector(task)
        assert isinstance(connector, SearXNGResultsConnector)
        assert connector.policy.endpoint == "http://127.0.0.1:8888/search"
        assert connector.observation_context is not None
        assert connector.observation_context.keyword_set.id == "example_ai_assistants"
    finally:
        executor.shutdown()
        engine.dispose()
