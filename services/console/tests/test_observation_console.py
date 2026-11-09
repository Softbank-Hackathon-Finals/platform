from starlette.testclient import TestClient
from test_kubernetes_workload import case as case

from bravo_console.observation_console import ObservationConsole


def test_standalone_console_denies_changes_enrollment_foreign_origin_and_revocation(case):
    _, _, source, _ = case
    authorized = {"value": True}
    app = ObservationConsole(
        source, origin="http://127.0.0.1:18123", authorize=lambda scope: authorized["value"]
    )
    # No lifespan/poller: keep these registered fixture observations fixed.
    client = TestClient(app, base_url=app.origin)
    assert client.get("/console/").status_code == 200
    assert "data-observation-only" in client.get("/console/").text
    assert client.get("/v1/observed-workload").status_code == 200
    assert client.post("/v1/plans/any/cutover", json={}).status_code == 405
    assert client.get("/v1/plans/any").status_code == 404
    assert client.get("/v1/observed-workload?producer=other").status_code == 400
    assert (
        client.get("/v1/observed-workload", headers={"Origin": "https://other.invalid"}).status_code
        == 403
    )
    authorized["value"] = False
    response = client.get("/v1/observed-workload")
    assert response.status_code == 403 and "service-chat" not in response.text
