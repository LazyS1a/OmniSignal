from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from omnisignal.api import create_app
from omnisignal.api.auth import ControlPrincipal, hash_control_token, load_control_principals
from omnisignal.config import Environment, Settings
from omnisignal.contracts import ConnectorFailure, ErrorCategory
from omnisignal.storage import (
    AuditEvent,
    Base,
    SourceControlCommand,
    SourceControlState,
    assert_source_enabled,
    create_database_engine,
)


OPERATOR_TOKEN = "operator-token-with-at-least-32-random-like-characters"
VIEWER_TOKEN = "viewer-token-with-at-least-32-random-like-characters-xx"


def _principal(actor: str, role: str, token: str) -> ControlPrincipal:
    return ControlPrincipal(actor=actor, role=role, token_sha256=hash_control_token(token))


def _registry(tmp_path: Path, *, status: str = "allowed") -> Path:
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        f"""registry_version: 1
updated_at: '2026-09-03'
sources:
  - id: fixture_source
    display_name: Fixture Source
    connector_class: api
    status: {status}
    authorization_basis: project fixture
    rate_budget_rpm: 30
    kill_switch: true
""",
        encoding="utf-8",
    )
    return path


def _app(tmp_path: Path, principals: tuple[ControlPrincipal, ...]):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'control.sqlite3').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(environment=Environment.TEST, database_url=database_url)
    app = create_app(
        settings=settings,
        engine=engine,
        registry_path=_registry(tmp_path),
        control_principals=principals,
    )
    return app, engine


async def _request(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)


def test_rotation_after_app_recreation_revokes_old_token_and_preserves_control(tmp_path: Path) -> None:
    new_token = "new-operator-fixture-token-for-rotation-at-least-32"
    app, engine = _app(tmp_path, (_principal("ops-one", "operator", OPERATOR_TOKEN),))
    body = {"enabled": False, "expected_version": 0, "confirmation": "DISABLE fixture_source", "reason": "rotation drill"}
    path = "/ops/sources/fixture_source/control"
    key = "rotation-request-00000001"
    first = asyncio.run(_request(app, "POST", path, json=body, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": key}))
    assert first.status_code == 200
    rotated, rotated_engine = _app(tmp_path, (_principal("ops-one", "operator", new_token),))
    stale = asyncio.run(_request(rotated, "POST", path, json=body, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": key}))
    assert stale.status_code == 401  # Authentication must precede idempotency replay.
    replay = asyncio.run(_request(rotated, "POST", path, json=body, headers={"Authorization": f"Bearer {new_token}", "Idempotency-Key": key}))
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    with Session(rotated_engine) as session:
        assert session.get(SourceControlState, "fixture_source").version == 1
        events = session.scalars(select(AuditEvent).where(AuditEvent.action == "source_control_changed")).all()
        assert len(events) == 1
    engine.dispose()
    rotated_engine.dispose()


def test_control_principals_load_only_hashed_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = hash_control_token(OPERATOR_TOKEN)
    monkeypatch.setenv(
        "OMNISIGNAL_CONTROL_PRINCIPALS_JSON",
        json.dumps([{"actor": "ops-one", "role": "operator", "token_sha256": digest}]),
    )

    principals = load_control_principals()

    assert principals == (ControlPrincipal(actor="ops-one", role="operator", token_sha256=digest),)
    assert OPERATOR_TOKEN not in repr(principals)


@pytest.mark.parametrize(
    "document",
    [
        [{"actor": "ops", "role": "owner", "token_sha256": "a" * 64}],
        [{"actor": "ops", "role": "operator", "token_sha256": "not-a-hash"}],
        [
            {"actor": "ops", "role": "operator", "token_sha256": "a" * 64},
            {"actor": "ops", "role": "admin", "token_sha256": "b" * 64},
        ],
    ],
)
def test_control_principals_reject_invalid_or_ambiguous_config(
    monkeypatch: pytest.MonkeyPatch, document: list[dict[str, str]]
) -> None:
    monkeypatch.setenv("OMNISIGNAL_CONTROL_PRINCIPALS_JSON", json.dumps(document))
    with pytest.raises(RuntimeError, match="control principal"):
        load_control_principals()


def test_control_api_fails_closed_without_server_configuration(tmp_path: Path) -> None:
    app, engine = _app(tmp_path, ())
    response = asyncio.run(
        _request(
            app,
            "POST",
            "/ops/sources/fixture_source/control",
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": "request-000000000001"},
            json={
                "enabled": False,
                "expected_version": 0,
                "confirmation": "DISABLE fixture_source",
                "reason": "planned maintenance",
            },
        )
    )
    assert response.status_code == 503
    assert "token" not in response.text.lower()
    engine.dispose()


def test_control_api_enforces_auth_role_confirmation_version_and_idempotency(tmp_path: Path) -> None:
    principals = (
        _principal("ops-one", "operator", OPERATOR_TOKEN),
        _principal("read-one", "viewer", VIEWER_TOKEN),
    )
    app, engine = _app(tmp_path, principals)
    path = "/ops/sources/fixture_source/control"
    body = {
        "enabled": False,
        "expected_version": 0,
        "confirmation": "DISABLE fixture_source",
        "reason": "planned maintenance",
    }

    async def scenario() -> dict[str, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                missing = await client.post(path, json=body, headers={"Idempotency-Key": "request-000000000001"})
                wrong = await client.post(
                    path,
                    json=body,
                    headers={"Authorization": "Bearer wrong-token-value-with-at-least-32-characters", "Idempotency-Key": "request-000000000001"},
                )
                viewer = await client.post(
                    path,
                    json=body,
                    headers={"Authorization": f"Bearer {VIEWER_TOKEN}", "Idempotency-Key": "request-000000000001"},
                )
                bad_confirmation = await client.post(
                    path,
                    json={**body, "confirmation": "yes"},
                    headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": "request-000000000002"},
                )
                success = await client.post(
                    path,
                    json=body,
                    headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": "request-000000000003"},
                )
                replay = await client.post(
                    path,
                    json=body,
                    headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": "request-000000000003"},
                )
                changed_payload = await client.post(
                    path,
                    json={**body, "reason": "different reason"},
                    headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": "request-000000000003"},
                )
                stale = await client.post(
                    path,
                    json={
                        "enabled": True,
                        "expected_version": 0,
                        "confirmation": "ENABLE fixture_source",
                        "reason": "maintenance complete",
                    },
                    headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", "Idempotency-Key": "request-000000000004"},
                )
                sources = await client.get("/ops/sources")
                whoami = await client.get(
                    "/ops/control/whoami", headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}
                )
        return {
            "missing": missing,
            "wrong": wrong,
            "viewer": viewer,
            "bad_confirmation": bad_confirmation,
            "success": success,
            "replay": replay,
            "changed_payload": changed_payload,
            "stale": stale,
            "sources": sources,
            "whoami": whoami,
        }

    responses = asyncio.run(scenario())
    assert responses["missing"].status_code == 401
    assert responses["wrong"].status_code == 401
    assert responses["viewer"].status_code == 403
    assert responses["bad_confirmation"].status_code == 400
    assert responses["success"].status_code == 200
    assert responses["success"].json()["control"] == {
        "effective_enabled": False,
        "version": 1,
        "updated_by": "ops-one",
    }
    assert responses["success"].json()["replayed"] is False
    assert responses["replay"].status_code == 200
    assert responses["replay"].json()["replayed"] is True
    assert responses["changed_payload"].status_code == 409
    assert responses["stale"].status_code == 409
    assert responses["sources"].json()["items"][0]["effective_enabled"] is False
    assert responses["sources"].json()["items"][0]["control_version"] == 1
    assert responses["whoami"].json() == {"actor": "ops-one", "role": "operator"}

    with Session(engine) as session:
        assert session.scalar(select(SourceControlCommand)) is not None
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "source_control_changed")
        ).all()
        assert len(events) == 1
        assert events[0].actor == "ops-one"
        assert events[0].detail["before_enabled"] is True
        assert events[0].detail["after_enabled"] is False
        assert OPERATOR_TOKEN not in json.dumps(events[0].detail)
    engine.dispose()


def test_disabled_source_guard_persists_and_fails_before_collection(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+pysqlite:///{(tmp_path / 'guard.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            SourceControlState(
                source_id="fixture_source",
                enabled=False,
                version=2,
                updated_by="ops-one",
                reason="planned maintenance",
            )
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(ConnectorFailure) as error:
            assert_source_enabled(session, "fixture_source")

    assert error.value.category == ErrorCategory.POLICY_VIOLATION
    assert "disabled" in str(error.value).lower()
    engine.dispose()


def test_two_sources_can_be_controlled_independently(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'independent.sqlite3').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    registry_path = tmp_path / "two_sources.yaml"
    registry_path.write_text(
        """registry_version: 1
updated_at: '2026-09-03'
sources:
  - id: source_one
    display_name: Source One
    connector_class: api
    status: allowed
    authorization_basis: project fixture
    kill_switch: true
  - id: source_two
    display_name: Source Two
    connector_class: html
    status: allowed
    authorization_basis: project fixture
    kill_switch: true
""",
        encoding="utf-8",
    )
    settings = Settings(environment=Environment.TEST, database_url=database_url)
    app = create_app(
        settings=settings,
        engine=engine,
        registry_path=registry_path,
        control_principals=(_principal("ops-one", "operator", OPERATOR_TOKEN),),
    )
    headers = {
        "Authorization": f"Bearer {OPERATOR_TOKEN}",
        "Content-Type": "application/json",
    }

    async def scenario() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post(
                    "/ops/sources/source_one/control",
                    headers={**headers, "Idempotency-Key": "independent-source-one-off"},
                    json={
                        "enabled": False,
                        "expected_version": 0,
                        "confirmation": "DISABLE source_one",
                        "reason": "independent control check",
                    },
                )
                assert first.status_code == 200
                middle = (await client.get("/ops/sources")).json()["items"]
                second = await client.post(
                    "/ops/sources/source_two/control",
                    headers={**headers, "Idempotency-Key": "independent-source-two-off"},
                    json={
                        "enabled": False,
                        "expected_version": 0,
                        "confirmation": "DISABLE source_two",
                        "reason": "independent control check",
                    },
                )
                assert second.status_code == 200
                final = (await client.get("/ops/sources")).json()["items"]
                return middle, final

    middle, final = asyncio.run(scenario())
    middle_states = {str(item["source_id"]): item["effective_enabled"] for item in middle}
    final_states = {str(item["source_id"]): item["effective_enabled"] for item in final}
    assert middle_states == {"source_one": False, "source_two": True}
    assert final_states == {"source_one": False, "source_two": False}
    with Session(engine) as session:
        states = session.scalars(select(SourceControlState).order_by(SourceControlState.source_id)).all()
        assert [(state.source_id, state.version) for state in states] == [
            ("source_one", 1),
            ("source_two", 1),
        ]
    engine.dispose()
