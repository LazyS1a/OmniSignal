from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from streamlit.testing.v1 import AppTest

from omnisignal.ops_ui.client import OpsApiClient, OpsApiError, OpsCsvExport
from omnisignal.ops_ui.pages import render_sources
from omnisignal.ops_ui.presentation import format_ratio, safe_api_label, status_meta


APP_PATH = Path(__file__).parents[1] / "src" / "omnisignal" / "ops_ui" / "app.py"


class CountingStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.chunks_read = 0
        self.closed = False

    def __iter__(self):
        for _ in range(1000):
            self.chunks_read += 1
            yield b"x" * 32

    def close(self) -> None:
        self.closed = True


def test_client_stops_unknown_length_response_before_reading_entire_body(respx_mock: respx.MockRouter) -> None:
    stream = CountingStream()
    respx_mock.get("http://127.0.0.1:8010/ops/summary").respond(200, stream=stream)
    with pytest.raises(OpsApiError, match="size limit"):
        OpsApiClient(max_response_bytes=64, retries=0).get_json("/ops/summary")
    assert stream.chunks_read == 3
    assert stream.closed


@pytest.mark.parametrize("headers", [{"Content-Length": "1000000"}, {"Content-Encoding": "gzip"}])
def test_client_rejects_large_or_compressed_headers_without_reading_body(respx_mock: respx.MockRouter, headers) -> None:
    stream = CountingStream()
    respx_mock.get("http://127.0.0.1:8010/ops/summary").respond(200, headers=headers, stream=stream)
    with pytest.raises(OpsApiError):
        OpsApiClient(max_response_bytes=64, retries=0).get_json("/ops/summary")
    assert stream.chunks_read == 0
    assert stream.closed


def test_client_does_not_read_server_error_body(respx_mock: respx.MockRouter) -> None:
    stream = CountingStream()
    respx_mock.get("http://127.0.0.1:8010/ops/summary").respond(503, stream=stream)
    with pytest.raises(OpsApiError, match="HTTP 503"):
        OpsApiClient(retries=0).get_json("/ops/summary")
    assert stream.chunks_read == 0
    assert stream.closed


def _render_source_control_fixture(role: str) -> None:
    from omnisignal.ops_ui.pages import render_sources

    def load(path: str, params=None):
        del params
        assert path == "/ops/sources"
        return {
            "registry_version": 1,
            "registry_updated_at": "2026-09-03",
            "total": 1,
            "items": [
                {
                    "source_id": "fixture_source",
                    "display_name": "Fixture Source",
                    "registry_status": "allowed",
                    "kill_switch": True,
                    "effective_enabled": True,
                    "control_version": 0,
                }
            ],
        }

    def control(**kwargs):
        return {"control": kwargs}

    render_sources(load, control=control, principal={"actor": "fixture", "role": role})


def _render_records_export_fixture() -> None:
    from omnisignal.ops_ui.client import OpsCsvExport
    from omnisignal.ops_ui.pages import render_records

    def load(path: str, params=None):
        del params
        assert path == "/ops/records"
        return {
            "total": 1,
            "items": [
                {
                    "normalized_id": "a" * 64,
                    "source_id": "fixture_source",
                    "source_record_id": "record-1",
                    "title": "Fixture",
                    "quality_status": "accepted",
                    "language": "en",
                    "text_length": 4,
                    "text_preview": "body",
                }
            ],
        }

    def prepare_export(**kwargs):
        assert kwargs == {
            "source_id": "",
            "quality_status": "",
            "q": "",
            "limit": 1000,
        }
        return OpsCsvExport(content=b"header\r\nvalue\r\n", rows=1)

    render_records(load, prepare_export=prepare_export)


def test_ops_client_retries_one_transient_failure_and_returns_object(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get("http://127.0.0.1:8010/ops/summary").mock(
        side_effect=[
            httpx.Response(503, text="temporary"),
            httpx.Response(200, json={"records": {"active_ingested": 35}}),
        ]
    )
    result = OpsApiClient(retry_delay_seconds=0).get_json("/ops/summary")

    assert result["records"]["active_ingested"] == 35
    assert route.call_count == 2


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///D:/data/omnisignal.db",
        "http://user:password@127.0.0.1:8010",
        "http://127.0.0.1:8010/private/path",
        "http://127.0.0.1:8010?token=hidden",
    ],
)
def test_ops_client_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="API base URL"):
        OpsApiClient(base_url=base_url)


def test_ops_client_only_accepts_bounded_ops_queries() -> None:
    client = OpsApiClient()

    with pytest.raises(ValueError, match="read-only /ops"):
        client.get_json("/health/ready")
    with pytest.raises(ValueError, match="limit"):
        client.get_json("/ops/records", params={"limit": 201})
    with pytest.raises(ValueError, match="offset"):
        client.get_json("/ops/records", params={"offset": 100_001})
    with pytest.raises(ValueError, match="window_hours"):
        client.get_json("/ops/summary", params={"window_hours": 721})


def test_ops_client_rejects_oversized_or_non_object_responses() -> None:
    client = OpsApiClient(max_response_bytes=64, retries=0)

    with respx.mock:
        respx.get("http://127.0.0.1:8010/ops/summary").mock(
            return_value=httpx.Response(200, content=b"x" * 65)
        )
        with pytest.raises(OpsApiError, match="response exceeded"):
            client.get_json("/ops/summary")

    with respx.mock:
        respx.get("http://127.0.0.1:8010/ops/summary").mock(
            return_value=httpx.Response(200, json=["not", "an", "object"])
        )
        with pytest.raises(OpsApiError, match="JSON object"):
            OpsApiClient(retries=0).get_json("/ops/summary")


def test_ops_client_error_does_not_echo_response_body() -> None:
    with respx.mock:
        respx.get("http://127.0.0.1:8010/ops/audit").mock(
            return_value=httpx.Response(500, text="api_token=must-not-leak")
        )
        with pytest.raises(OpsApiError) as error:
            OpsApiClient(retries=0).get_json("/ops/audit")

    assert "must-not-leak" not in str(error.value)
    assert "HTTP 500" in str(error.value)


def test_ops_control_client_sends_ephemeral_token_and_never_retries_write(
    respx_mock: respx.MockRouter,
) -> None:
    token = "temporary-control-token-that-is-long-enough"
    route = respx_mock.post("http://127.0.0.1:8010/ops/sources/fixture_source/control").mock(
        return_value=httpx.Response(503, text=f"must-not-leak:{token}")
    )

    with pytest.raises(OpsApiError) as error:
        OpsApiClient(retries=2).set_source_control(
            source_id="fixture_source",
            enabled=False,
            expected_version=0,
            confirmation="DISABLE fixture_source",
            reason="planned maintenance",
            idempotency_key="request-000000000001",
            bearer_token=token,
        )

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {token}"
    assert request.headers["Idempotency-Key"] == "request-000000000001"
    assert token not in str(error.value)
    assert "HTTP 503" in str(error.value)


def test_ops_csv_client_is_bounded_and_validates_response(
    respx_mock: respx.MockRouter,
) -> None:
    content = b"\xef\xbb\xbfsource_id\r\nfixture_source\r\n"
    route = respx_mock.get("http://127.0.0.1:8010/ops/exports/records.csv").mock(
        return_value=httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "text/csv; charset=utf-8", "X-OmniSignal-Export-Rows": "1"},
        )
    )

    exported = OpsApiClient().download_records_csv(
        source_id="fixture_source",
        quality_status="accepted",
        q="needle",
        limit=25,
    )

    assert exported == OpsCsvExport(content=content, rows=1)
    assert route.call_count == 1
    assert route.calls[0].request.headers["Accept"] == "text/csv"
    assert dict(route.calls[0].request.url.params) == {
        "source_id": "fixture_source",
        "quality_status": "accepted",
        "q": "needle",
        "limit": "25",
    }

    with pytest.raises(ValueError, match="limit"):
        OpsApiClient().download_records_csv(limit=1001)


def test_ops_csv_client_rejects_oversized_or_invalid_exports(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get("http://127.0.0.1:8010/ops/exports/records.csv")
    route.mock(
        return_value=httpx.Response(
            200,
            content=b"x" * 9,
            headers={"Content-Type": "text/csv", "X-OmniSignal-Export-Rows": "1"},
        )
    )
    with pytest.raises(OpsApiError, match="size limit"):
        OpsApiClient(max_response_bytes=8, retries=0).download_records_csv()

    route.mock(
        return_value=httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Type": "application/json", "X-OmniSignal-Export-Rows": "1"},
        )
    )
    with pytest.raises(OpsApiError, match="CSV"):
        OpsApiClient(retries=0).download_records_csv()


def test_ops_csv_client_retries_read_failure_without_echoing_body(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get("http://127.0.0.1:8010/ops/exports/records.csv").mock(
        return_value=httpx.Response(503, text="database-password=must-not-leak")
    )

    with pytest.raises(OpsApiError) as error:
        OpsApiClient(retry_delay_seconds=0).download_records_csv()

    assert route.call_count == 2
    assert "must-not-leak" not in str(error.value)
    assert "HTTP 503" in str(error.value)


def test_ops_ui_presentation_helpers_keep_metric_scope_visible() -> None:
    assert format_ratio({"ratio": 0.875, "numerator": 7, "denominator": 8}) == "87.5% · 7/8"
    assert format_ratio({"ratio": None, "numerator": 0, "denominator": 0}) == "暂无可用分母 · 0/0"
    assert status_meta("SUCCEEDED") == ("成功", "success")
    assert status_meta("experimental") == ("experimental", "neutral")
    assert safe_api_label("http://127.0.0.1:8010") == "127.0.0.1:8010"


def test_ops_ui_overview_renders_live_api_data_without_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    api_base_url = "http://127.0.0.1:18010"
    monkeypatch.setenv("OMNISIGNAL_API_BASE_URL", api_base_url)
    respx_mock.get(f"{api_base_url}/ops/summary", params={"window_hours": 24}).mock(
        return_value=httpx.Response(
            200,
            json={
                "records": {"active_ingested": 35, "normalized_versions": 35},
                "ingestion_runs": {"total": 2, "stopped": 1},
                "plugin_attempts": 3,
                "archive_coverage": {
                    "ratio": 1.0,
                    "numerator": 35,
                    "denominator": 35,
                    "scope": "active ingested records",
                },
                "quality_statuses": {"accepted": 34, "quarantined": 1},
            },
        )
    )

    app = AppTest.from_file(APP_PATH).run(timeout=10)

    assert not app.exception
    assert not app.error
    assert [metric.value for metric in app.metric] == ["35", "35", "2", "1", "3"]
    assert any("active ingested records" in caption.value for caption in app.caption)


def test_ops_ui_shows_api_failure_instead_of_demo_metrics(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    api_base_url = "http://127.0.0.1:18011"
    monkeypatch.setenv("OMNISIGNAL_API_BASE_URL", api_base_url)
    route = respx_mock.get(f"{api_base_url}/ops/summary", params={"window_hours": 24}).mock(
        return_value=httpx.Response(503, text="upstream-secret-must-not-appear")
    )

    app = AppTest.from_file(APP_PATH).run(timeout=10)

    assert not app.exception
    assert len(app.metric) == 0
    assert len(app.error) == 1
    assert "HTTP 503" in app.error[0].value
    assert "upstream-secret" not in app.error[0].value
    assert route.call_count == 2


def test_ops_ui_manual_refresh_clears_cached_summary(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    api_base_url = "http://127.0.0.1:18012"
    monkeypatch.setenv("OMNISIGNAL_API_BASE_URL", api_base_url)
    route = respx_mock.get(f"{api_base_url}/ops/summary", params={"window_hours": 24}).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "records": {"active_ingested": 1, "normalized_versions": 1},
                    "ingestion_runs": {"total": 1, "stopped": 0},
                    "plugin_attempts": 0,
                    "archive_coverage": {"ratio": 1.0, "numerator": 1, "denominator": 1},
                    "quality_statuses": {},
                },
            ),
            httpx.Response(
                200,
                json={
                    "records": {"active_ingested": 2, "normalized_versions": 2},
                    "ingestion_runs": {"total": 2, "stopped": 0},
                    "plugin_attempts": 0,
                    "archive_coverage": {"ratio": 1.0, "numerator": 2, "denominator": 2},
                    "quality_statuses": {},
                },
            ),
        ]
    )
    app = AppTest.from_file(APP_PATH).run(timeout=10)
    assert app.metric[0].value == "1"

    app.button[0].click().run(timeout=10)

    assert not app.exception
    assert app.metric[0].value == "2"
    assert route.call_count == 2


def test_ops_ui_clear_control_token_uses_widget_callback(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    api_base_url = "http://127.0.0.1:18013"
    token = "temporary-control-token-that-is-long-enough"
    monkeypatch.setenv("OMNISIGNAL_API_BASE_URL", api_base_url)
    respx_mock.get(f"{api_base_url}/ops/summary", params={"window_hours": 24}).mock(
        return_value=httpx.Response(
            200,
            json={
                "records": {"active_ingested": 0, "normalized_versions": 0},
                "ingestion_runs": {"total": 0, "stopped": 0},
                "plugin_attempts": 0,
                "archive_coverage": {"ratio": None, "numerator": 0, "denominator": 0},
                "quality_statuses": {},
            },
        )
    )
    respx_mock.get(f"{api_base_url}/ops/control/whoami").mock(
        return_value=httpx.Response(200, json={"actor": "fixture", "role": "operator"})
    )

    app = AppTest.from_file(APP_PATH).run(timeout=10)
    app.text_input[0].input(token).run(timeout=10)
    clear_button = next(button for button in app.button if button.label == "清除操作令牌")
    clear_button.click().run(timeout=10)

    assert not app.exception
    assert app.text_input[0].value == ""
    assert not any(button.label == "清除操作令牌" for button in app.button)


def test_source_control_form_is_visible_only_to_operator_roles() -> None:
    operator = AppTest.from_function(
        _render_source_control_fixture, args=("operator",)
    ).run(timeout=10)
    viewer = AppTest.from_function(
        _render_source_control_fixture, args=("viewer",)
    ).run(timeout=10)

    assert not operator.exception
    assert any(code.value == "DISABLE fixture_source" for code in operator.code)
    assert any(button.label == "停用后续运行" for button in operator.button)
    assert not viewer.exception
    assert not viewer.code
    assert not any(button.label == "停用后续运行" for button in viewer.button)


def test_records_page_prepares_bounded_csv_download() -> None:
    app = AppTest.from_function(_render_records_export_fixture).run(timeout=10)
    prepare = next(button for button in app.button if button.label == "准备当前筛选 CSV")
    prepare.click().run(timeout=10)

    assert not app.exception
    assert any(message.value == "已准备 1 行标准记录。" for message in app.success)
    downloads = app.get("download_button")
    assert len(downloads) == 1
    assert downloads[0].label == "下载 CSV"
