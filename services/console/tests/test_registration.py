"""Real T10 registration and plan storage through the console browser boundary."""

import pytest
from registration_fixture import SOURCE, TARGET, WORKLOAD, install
from test_app import CHANGE, client, setup


@pytest.fixture
def registration(tmp_path):
    fixture = setup(tmp_path / "state.db")
    app, store, state, _ = fixture
    install(app.api, store, state, tmp_path)
    return fixture


def test_registration_flow_uses_real_registry_without_execution(registration):
    _, store, state, _ = registration
    with client(registration) as c:
        for body, key in [(SOURCE, "source"), (TARGET, "target")]:
            result = c.post(
                "/v1/environments", json=body, headers=CHANGE | {"Idempotency-Key": key}
            )
            assert result.status_code == 201
            assert result.json()["checks"]["warnings"] == ["write_scope_unknown"]
        app = c.post("/v1/workloads", json=WORKLOAD, headers=CHANGE | {"Idempotency-Key": "app"})
        assert app.status_code == 201
        assert app.json()["repo_deploy_match"] is True
        body = dict(workload_id=app.json()["workload_id"], target_environment_id="env-target")
        headers = CHANGE | {"Idempotency-Key": "plan"}
        created = c.post("/v1/plans", json=body, headers=headers)
        assert created.status_code == 201
        value = created.json()
        assert value["state"] == "COLLECT" and value["version"] == 1
        # Response-loss recovery retains the original key and receipt, even if later probes fail.
        state["registration_probe_denied"] = True
        replay = c.post("/v1/plans", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == value
        plan = c.get("/v1/plans/" + value["plan_id"])
        assert plan.status_code == 200 and plan.json()["state"] == "COLLECT"
        assert len(store.read_after(value["plan_id"], None)) == 1
        assert state["submitted"] == 0
        assert (
            c.post(
                "/v1/plans", json=body | {"target_environment_id": "other"}, headers=headers
            ).status_code
            == 409
        )
        state["allowed"] = False
        assert c.post("/v1/plans", json=body, headers=headers).status_code == 403


@pytest.mark.parametrize("path", ["/v1/environments", "/v1/workloads", "/v1/plans"])
@pytest.mark.parametrize(
    "fault", ["missing-origin", "wrong-origin", "no-boundary", "cross-site", "too-large"]
)
def test_all_registration_posts_keep_browser_boundary(registration, path, fault):
    headers = dict(CHANGE)
    if fault == "missing-origin":
        headers.pop("Origin")
    if fault == "wrong-origin":
        headers["Origin"] = "https://foreign.invalid"
    if fault == "no-boundary":
        headers.pop("X-Bravo-Console")
    if fault == "cross-site":
        headers["Sec-Fetch-Site"] = "cross-site"
    with client(registration) as c:
        assert c.post(
            path, content="x" * 65537 if fault == "too-large" else "{}", headers=headers
        ).status_code in (403, 413)
    with registration[1]._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0


def test_unregistered_reference_and_credentials_do_not_reach_probe(registration):
    with client(registration) as c:
        for body, code in [
            (SOURCE | {"connection_ref": "unknown"}, 404),
            (SOURCE | {"token": "not-a-real-token"}, 422),
        ]:
            response = c.post("/v1/environments", json=body, headers=CHANGE)
            assert response.status_code == code
        assert c.get("/v1/environments").status_code == 405
        assert c.post("/v1/environments/anything", json={}, headers=CHANGE).status_code == 404


def test_missing_host_registration_routes_are_not_fabricated(tmp_path):
    fixture = setup(tmp_path / "state.db")
    with client(fixture) as c:
        response = c.post("/v1/environments", json=SOURCE, headers=CHANGE)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"]
