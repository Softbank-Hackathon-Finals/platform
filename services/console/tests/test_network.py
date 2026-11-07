"""Real plan authorization / ASGI / HTTP / WebSocket, synthetic network inputs."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_app import CHANGE, ORIGIN, setup

from bravo_console.network import (
    SOURCE_IDS,
    AtlasBinding,
    AtlasClient,
    PlanNetworkAtlas,
    project_snapshot,
)


def snapshot(now=None):
    now = now or datetime.now(UTC)
    at = now.isoformat()
    return {
        "schema_version": 1,
        "cycle_id": "fixture-cycle",
        "received_at": at,
        "sources": [
            {
                "id": identity,
                "status": "ok",
                "checked_at": at,
                "last_success_at": at,
                "scope": "Synthetic read-only test",
                "duration_ms": 1,
                "retained": False,
                "data": {
                    "resources": [
                        {
                            "kind": "Service",
                            "namespace": "fixture",
                            "name": "fixture-service",
                            "status": "UNKNOWN",
                            "ip": "192.0.2.1",
                            "private_extra": "discard",
                        }
                    ],
                    "networks": [],
                    "links": [],
                },
            }
            for identity in sorted(SOURCE_IDS)
        ],
        "activity": [],
        "activity_status": "not_connected",
    }


def install(app, state):
    class Client:
        calls = 0

        async def read(self):
            self.calls += 1
            if state.get("raise_private"):
                raise RuntimeError("PRIVATE_PROVIDER_DIAGNOSTIC")
            if state.get("revoke_during_read"):
                state["network_allowed"] = False
            return state.get("network_document", snapshot())

    state["network_allowed"] = True
    provider = Client()
    app.network = PlanNetworkAtlas(
        app.api,
        provider,
        authorize=lambda *_: state["network_allowed"],
        registration=lambda *_: AtlasBinding("env-source", "env-target"),
    )
    return provider


def client(app):
    return TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"})


def test_registered_network_is_a_separately_authorized_read_not_a_gate(tmp_path):
    app, _, state, _ = setup(tmp_path / "state.db")
    provider = install(app, state)
    with client(app) as c:
        r = c.get("/v1/plans/plan-demo/network-status")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["sova_context"]["authorizes_cutover"] is False
        assert body["sova_context"]["scope"] == "shared-environment-network"
        assert "private_extra" not in r.text
        state["network_allowed"] = False
        assert c.get("/v1/plans/plan-demo/network-status").status_code == 403
        assert provider.calls == 1  # no upstream call without network-wide grant
    assert state["submitted"] == 0


def test_registration_and_permission_are_checked_again_after_external_read(tmp_path):
    app, _, state, _ = setup(tmp_path / "state.db")
    install(app, state)
    with client(app) as c:
        app.network.registration = lambda *_: AtlasBinding("other-source", "env-target")
        assert c.get("/v1/plans/plan-demo/network-status").status_code == 503
        app.network.registration = lambda *_: AtlasBinding("env-source", "env-target")
        state["revoke_during_read"] = True
        result = c.get("/v1/plans/plan-demo/network-status")
        assert result.status_code == 403 and "fixture-service" not in result.text


def test_native_plan_auth_query_and_write_guards(tmp_path):
    app, _, state, _ = setup(tmp_path / "state.db")
    provider = install(app, state)
    with client(app) as c:
        assert (
            c.get("/v1/plans/plan-demo/network-status?origin=https://else.invalid").status_code
            == 400
        )
        assert (
            c.post("/v1/plans/plan-demo/network-status", json={}, headers=CHANGE).status_code == 405
        )
        assert (
            c.get(
                "/v1/plans/plan-demo/network-status", headers={"Origin": "https://else.invalid"}
            ).status_code
            == 403
        )
        state["allowed"] = False
        assert c.get("/v1/plans/plan-demo/network-status").status_code == 403
        assert provider.calls == 0


def test_old_atlas_url_only_returns_to_the_native_shell(tmp_path):
    app, _, _, _ = setup(tmp_path / "state.db")
    with client(app) as c:
        response = c.get("/console/network/?plan=plan-demo", follow_redirects=False)
        assert response.headers["location"] == "/console/?view=network&plan=plan-demo"
        assert response.headers["cache-control"] == "no-store"
        unsafe = c.get("/console/network/?plan=//other.invalid", follow_redirects=False)
        assert unsafe.headers["location"] == "/console/?view=network"
        assert c.post("/console/network/", json={}, headers=CHANGE).status_code == 405


def test_unconfigured_and_provider_failure_do_not_expose_diagnostics(tmp_path):
    app, _, state, _ = setup(tmp_path / "state.db")
    with client(app) as c:
        assert c.get("/v1/plans/plan-demo/network-status").status_code == 503
        install(app, state)
        state["raise_private"] = True
        result = c.get("/v1/plans/plan-demo/network-status")
        assert result.status_code == 503 and "PRIVATE" not in result.text


def test_failed_source_retains_original_success_time_and_stale_values():
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    body = snapshot(now - timedelta(minutes=10))
    source = body["sources"][0]
    source.update(status="error", error_code="TIMEOUT", retained=True, last_success_at=old)
    output = project_snapshot(body, now)
    assert output["received_at"] == body["received_at"]
    assert output["sources"][0]["last_success_at"] == old
    assert output["sources"][0]["retained"] is True
    assert output["sources"][0]["status"] == "error"
    assert output["server_now"] != output["received_at"]


@pytest.mark.parametrize(
    "fault", ["duplicate", "missing", "future", "secret", "retained", "activity", "nan"]
)
def test_invalid_upstream_never_becomes_clean_data(fault):
    body = deepcopy(snapshot())
    if fault == "duplicate":
        body["sources"][1] = body["sources"][0]
    elif fault == "missing":
        body["sources"] = []
    elif fault == "future":
        body["sources"][0]["checked_at"] = "2099-01-01T00:00:00Z"
    elif fault == "secret":
        body["sources"][0]["data"]["resources"][0]["name"] = "Bearer synthetic-secret"
    elif fault == "retained":
        body["sources"][0].update(retained=True, last_success_at=None)
    elif fault == "nan":
        body["sources"][0]["duration_ms"] = float("nan")
    else:
        body["activity"] = [{"stage": "delete", "status": "done", "reason_code": "COMPLETE"}]
    with pytest.raises(ValueError):
        project_snapshot(body, datetime.now(UTC))


def test_websocket_uses_same_plan_auth_and_closes_when_network_grant_is_revoked(tmp_path):
    app, _, state, _ = setup(tmp_path / "state.db")
    install(app, state)
    with (
        client(app) as c,
        c.websocket_connect(
            "wss://console.example.invalid/v1/plans/plan-demo/network-stream",
            headers={"Origin": ORIGIN},
        ) as ws,
    ):
        body = ws.receive_json()
        assert body["type"] == "snapshot"
        assert body["data"]["sova_context"]["plan_id"] == "plan-demo"
        state["network_allowed"] = False
        ws.send_text("refresh")
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
        assert err.value.code == 4403
    assert state["submitted"] == 0


@pytest.mark.parametrize("origin", [None, "https://else.invalid"])
def test_websocket_cross_origin_and_missing_origin_rejected_without_upstream(tmp_path, origin):
    app, _, state, _ = setup(tmp_path / "state.db")
    provider = install(app, state)
    with client(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "/v1/plans/plan-demo/network-stream", headers={"Origin": origin} if origin else {}
            ):
                pass
    assert provider.calls == 0


def test_public_fixed_client_does_not_forward_browser_credentials_or_follow_redirects():
    asyncio.run(check_fixed_client())


async def check_fixed_client():
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json=snapshot())

    service = AtlasClient("https://atlas.example.invalid")
    await service.client.aclose()
    service.client = httpx.AsyncClient(
        base_url=service.origin, transport=httpx.MockTransport(reply)
    )
    try:
        await service.read()
        await service.read()
        assert len(requests) == 1
        assert requests[0].url.path == "/api/status"
        assert "authorization" not in requests[0].headers and "cookie" not in requests[0].headers
    finally:
        await service.aclose()
