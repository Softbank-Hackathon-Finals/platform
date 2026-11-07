"""Read-only producer registration through the existing T10 host factory."""

import pytest
from bravo_platform_events.plan_api import Principal
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup
from test_workload import install

from bravo_console.composition import compose_console


def compose(case, **options):
    app, store, state, _ = case
    return compose_console(
        app.api,
        store,
        public_origin=ORIGIN,
        resolve_principal=lambda request: (
            Principal("fixture-operator", "tenant-demo")
            if state["allowed"] and request.cookies.get("fixture-session") == "test-only"
            else None
        ),
        authorize_panel=lambda *_: False,
        authorize_operation=lambda *_: False,
        **options,
    )


def test_omitted_workload_provider_stays_unavailable(tmp_path):
    case = setup(tmp_path / "state.db")
    with TestClient(compose(case), base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        assert c.get("/v1/plans/plan-demo/workload").status_code == 503
    assert case[2]["submitted"] == 0


def test_registered_reader_reuses_native_plan_identity_and_no_execution(tmp_path):
    case = setup(tmp_path / "state.db")
    reader = install(case[0], case[2])
    with TestClient(
        compose(case, workload=reader), base_url=ORIGIN, cookies={"fixture-session": "test-only"}
    ) as c:
        native = c.get("/v1/plans/plan-demo").json()
        response = c.get("/v1/plans/plan-demo/workload")
        assert response.status_code == 200
        assert response.json()["plan_digest"] == native["plan_digest"]
        assert response.json()["plan_version"] == native["version"]
        assert response.json()["authorizes_execution"] is False
        assert c.post("/v1/plans/plan-demo/workload", json={}).status_code == 405
    assert case[2]["submitted"] == 0


@pytest.mark.parametrize("permission", ["allowed", "workload_allowed", "producer_allowed"])
def test_composition_does_not_bypass_any_existing_read_authority(tmp_path, permission):
    case = setup(tmp_path / "state.db")
    reader = install(case[0], case[2])
    case[2][permission] = False
    with TestClient(
        compose(case, workload=reader), base_url=ORIGIN, cookies={"fixture-session": "test-only"}
    ) as c:
        response = c.get("/v1/plans/plan-demo/workload")
    assert response.status_code in (401, 403, 503)
    assert "synthetic-api" not in response.text
    assert case[2]["submitted"] == 0


def test_reader_bound_to_another_t10_instance_is_rejected_before_serving(tmp_path):
    current = setup(tmp_path / "current.db")
    other = setup(tmp_path / "other.db")
    reader = install(other[0], other[2])
    with pytest.raises(ValueError, match="requires_same_t10_api"):
        compose(current, workload=reader)
    assert not other[2].get("workload_reads")


def test_arbitrary_provider_object_is_not_discovered_or_called(tmp_path):
    case = setup(tmp_path / "state.db")
    with pytest.raises(ValueError, match="requires_same_t10_api"):
        compose(case, workload=lambda *_: None)
