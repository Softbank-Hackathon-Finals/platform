"""Real owner document/projector integrations; all deployment identities are fixtures."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from expanded_fixture import setup_expanded
from fastapi.testclient import TestClient
from test_app import CHANGE, ORIGIN

from bravo_console.workspace_data import FEATURES


def client(case):
    return TestClient(case[0], base_url=ORIGIN, cookies={"fixture-session": "test-only"})


def test_all_nine_panels_use_owner_documents_and_keep_unknown_costs_unknown(tmp_path):
    case = setup_expanded(tmp_path / "state.db")
    with client(case) as c:
        response = c.get("/v1/plans/plan-expanded/workspace")
    assert response.status_code == 200, response.text
    value = response.json()
    assert set(value["features"]) == set(FEATURES)
    assert {key: item["state"] for key, item in value["features"].items()} == dict.fromkeys(
        FEATURES, "OK"
    )
    assert value["authorizes_cutover"] is False
    assert value["operations"]["actions"]["shift"] is True
    costs = value["features"]["costs"]["tables"][0]["rows"]
    assert costs[2] == ["target", "미관측", "미관측"]
    audit = value["features"]["audit"]["tables"][0]["rows"]
    assert audit[0][4] == "DESTRUCTIVE_ATTEMPT_BLOCKED"
    assert "aws s3 rb" not in response.text
    monitoring = value["features"]["monitoring"]["tables"]
    assert any("23" in row for table in monitoring for row in table["rows"])
    assert any("BravoFixture" in row for table in monitoring for row in table["rows"])


@pytest.mark.parametrize(
    "fault", ["digest", "plan", "version", "expired", "source", "bytes", "internal_digest"]
)
def test_registered_artifact_failure_does_not_poison_other_panels(tmp_path, fault):
    case = setup_expanded(tmp_path / "state.db")
    old = case[3]["inventory"]
    changes = {
        "digest": {"content_sha256": "f" * 64},
        "plan": {"plan_id": "other-plan"},
        "version": {"plan_version": 999},
        "expired": {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
        "source": {"source_revision": "x" * 40},
        "bytes": {"document": b'{"a":1,"a":2}'},
    }
    if fault == "internal_digest":
        import hashlib

        body = json.loads(old.document)
        body["digest"] = "sha256:" + "f" * 64
        raw = json.dumps(body).encode()
        changes[fault] = {"document": raw, "content_sha256": hashlib.sha256(raw).hexdigest()}
    case[3]["inventory"] = replace(old, **changes[fault])
    with client(case) as c:
        value = c.get("/v1/plans/plan-expanded/workspace").json()
    assert value["features"]["inventory"]["state"] == "UNKNOWN"
    assert value["features"]["inventory"]["tables"] == []
    assert value["features"]["costs"]["state"] == "OK"


def test_revoked_source_registration_removes_every_result(tmp_path):
    case = setup_expanded(tmp_path / "state.db")
    with client(case) as c:
        assert (
            c.get("/v1/plans/plan-expanded/workspace").json()["features"]["audit"]["state"] == "OK"
        )
        case[2]["source_allowed"] = False
        result = c.get("/v1/plans/plan-expanded/workspace")
    assert all(
        row["state"] == "FORBIDDEN" and not row["tables"]
        for row in result.json()["features"].values()
    )
    assert "run-fixture" not in result.text


def test_account_cost_permission_is_separate_from_plan_read(tmp_path):
    case = setup_expanded(tmp_path / "state.db")
    case[0].workspace.authorize = lambda _scope, _plan, feature: feature != "costs"
    with client(case) as c:
        value = c.get("/v1/plans/plan-expanded/workspace").json()
    assert value["features"]["costs"]["state"] == "FORBIDDEN"
    assert value["features"]["inventory"]["state"] == "OK"


@pytest.mark.parametrize("stamp", ["expired", "invalid"])
def test_source_expiry_is_rechecked_after_other_providers_complete(tmp_path, stamp):
    case = setup_expanded(tmp_path / "state.db")
    now = datetime.now(UTC)
    case[0].workspace.clock = lambda: now
    case[0].workspace.sources["costs"] = lambda *_: {
        "tables": [],
        "valid_until": now.isoformat() if stamp == "expired" else "not-a-time",
    }
    with client(case) as c:
        value = c.get("/v1/plans/plan-expanded/workspace").json()
    assert value["features"]["costs"] == {
        "state": "UNKNOWN",
        "reason": "source_expired",
        "tables": [],
    }
    assert value["features"]["audit"]["state"] == "OK"


def test_workspace_is_no_store_and_refuses_query_post_foreign_origin_and_revoked_login(tmp_path):
    case = setup_expanded(tmp_path / "state.db")
    with client(case) as c:
        assert c.get("/v1/plans/plan-expanded/workspace").headers["cache-control"] == "no-store"
        assert c.get("/v1/plans/plan-expanded/workspace?path=/etc/passwd").status_code == 400
        assert (
            c.post("/v1/plans/plan-expanded/workspace", json={}, headers=CHANGE).status_code == 405
        )
        assert (
            c.get(
                "/v1/plans/plan-expanded/workspace", headers={"Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        case[2]["allowed"] = False
        assert c.get("/v1/plans/plan-expanded/workspace").status_code == 403


def test_owner_diagnostics_never_appear_in_error_json(tmp_path):
    case = setup_expanded(tmp_path / "state.db")

    def broken(*_):
        raise RuntimeError("PRIVATE_TOKEN_AND_LOCAL_PATH")

    case[0].workspace.sources["inventory"] = broken
    with client(case) as c:
        response = c.get("/v1/plans/plan-expanded/workspace")
    assert "PRIVATE" not in response.text
    assert response.json()["features"]["inventory"]["state"] == "UNKNOWN"


def test_provider_text_is_not_html_and_secret_like_strings_are_redacted(tmp_path):
    case = setup_expanded(tmp_path / "state.db")
    case[0].workspace.sources["ai"] = lambda *_: {
        "tables": [
            {
                "title": "Untrusted text",
                "columns": ["value"],
                "rows": [
                    ["<img src=x onerror=alert(1)>"],
                    ["password=synthetic-value-for-redaction"],
                ],
            }
        ]
    }
    with client(case) as c:
        result = c.get("/v1/plans/plan-expanded/workspace")
    rows = result.json()["features"]["ai"]["tables"][0]["rows"]
    assert rows[0][0] == "<img src=x onerror=alert(1)>"
    assert rows[1][0] == "[REDACTED]"


def test_late_provider_cannot_return_panels_for_an_advanced_plan(tmp_path):
    case = setup_expanded(tmp_path / "state.db")

    def change(*_):
        with case[1]._transaction(write=True, plan_id="plan-expanded") as c:
            c.execute("UPDATE plans SET version=version+1 WHERE plan_id='plan-expanded'")
        return {"tables": []}

    case[0].workspace.sources["exit"] = change
    with client(case) as c:
        response = c.get("/v1/plans/plan-expanded/workspace")
    assert (
        response.status_code == 503
        and response.json()["detail"]["code"] == "workspace_scope_changed"
    )


def test_one_unavailable_permission_provider_does_not_hide_other_panels(tmp_path):
    case = setup_expanded(tmp_path / "state.db")

    def authorize(_scope, _plan, feature):
        if feature == "costs":
            raise RuntimeError("PRIVATE_IDENTITY_ERROR")
        return True

    case[0].workspace.authorize = authorize
    with client(case) as c:
        response = c.get("/v1/plans/plan-expanded/workspace")
    assert response.status_code == 200 and "PRIVATE" not in response.text
    assert response.json()["features"]["costs"]["state"] == "UNKNOWN"
    assert response.json()["features"]["audit"]["state"] == "OK"


def test_deployment_composition_uses_same_native_services_and_explicit_providers(tmp_path):
    from bravo_platform_events.plan_api import Principal

    from bravo_console.composition import compose_console

    case = setup_expanded(tmp_path / "state.db")
    native = case[0].workspace.operations
    wrapped = compose_console(
        case[0].api,
        case[1],
        public_origin=ORIGIN,
        resolve_principal=lambda request: (
            Principal("fixture-operator", "tenant-demo")
            if request.cookies.get("fixture-session") == "test-only"
            else None
        ),
        authorize_panel=lambda *_: True,
        authorize_operation=lambda *_: True,
        registered_loaders={"costs": lambda *_: case[3]["costs"]},
        authenticate_registration=lambda *_: True,
        shift=native.shift,
        failback=native.failback,
        rehearsal=native.rehearsal,
        rehearsal_tasks=lambda *_: ["task-registered"],
    )
    with TestClient(wrapped, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        document = c.get("/v1/plans/plan-expanded/workspace").json()
    assert document["features"]["costs"]["state"] == "OK"
    assert document["features"]["monitoring"]["state"] == "UNCONFIGURED"
    assert document["operations"]["actions"]["shift"] is True
