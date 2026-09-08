from __future__ import annotations

from pathlib import Path

import httpx

from omnisignal.collection_jobs import CollectionTaskCatalog
from omnisignal.collection_readiness import CollectionReadinessService


ROOT = Path(__file__).parents[1]


def _task():
    catalog = CollectionTaskCatalog(ROOT / "config" / "collection_tasks.yaml", ROOT)
    return catalog, catalog.tasks["searxng_results_example"]


def test_searxng_readiness_uses_local_config_without_searching() -> None:
    catalog, task = _task()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"version": "fixture", "engines": [
                {"name": "duckduckgo", "enabled": True}, {"name": "brave", "enabled": True}
            ]},
        )

    service = CollectionReadinessService(catalog, transport=httpx.MockTransport(handler))
    result = service.check(task, force=True)

    assert result.available is True and result.detail_code == "searxng_ready"
    assert len(requests) == 1
    assert requests[0].url == httpx.URL("http://127.0.0.1:8888/config")
    assert "q" not in requests[0].url.params


def test_searxng_readiness_fails_closed_for_unavailable_or_disabled_engine() -> None:
    catalog, task = _task()
    unavailable = CollectionReadinessService(
        catalog,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="private upstream detail")),
    ).check(task, force=True)
    disabled = CollectionReadinessService(
        catalog,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"version": "fixture", "engines": [{"name": "duckduckgo", "enabled": False}]},
            )
        ),
    ).check(task, force=True)

    assert unavailable.available is False and unavailable.detail_code == "dependency_unavailable"
    assert "private upstream detail" not in unavailable.message
    assert disabled.available is False and disabled.detail_code == "engine_unavailable"


def test_non_searxng_task_is_ready_without_network() -> None:
    catalog, _ = _task()

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("non-SearXNG readiness must not make an HTTP request")

    service = CollectionReadinessService(catalog, transport=httpx.MockTransport(fail_if_called))
    result = service.check(catalog.tasks["public_search_signals_example"], force=True)

    assert result.available is True and result.detail_code == "configured"


def test_timeout_and_oversize_fail_closed():
    catalog, task = _task()
    def timeout(request):
        raise httpx.ReadTimeout("fixture", request=request)
    for handler in (timeout, lambda request: httpx.Response(200, content=b"x" * 512_001)):
        service = CollectionReadinessService(catalog, transport=httpx.MockTransport(handler))
        assert service.check(task, force=True).available is False


def test_dual_engine_task_rejects_missing_second_engine():
    catalog, task = _task()
    service = CollectionReadinessService(catalog, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"version": "fixture", "engines": [
            {"name": "duckduckgo", "enabled": True}
        ]})
    ))
    assert service.check(task, force=True).detail_code == "engine_unavailable"


def test_searxng_task_accepts_versioned_entity_binding(tmp_path):
    import yaml
    document = yaml.safe_load((ROOT / "config/collection_tasks.yaml").read_text(encoding="utf-8"))
    document["tasks"][-1]["entity_set"] = {"id": "example_note_tools", "version": 1}
    path = tmp_path / "tasks.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    catalog = CollectionTaskCatalog(path, ROOT)
    context = catalog.observation_context(catalog.tasks["searxng_results_example"])
    assert context.entity_set.id == "example_note_tools"
