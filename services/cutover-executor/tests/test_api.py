from fastapi.testclient import TestClient

from bravo_cutover.api import create_app


def test_healthz():
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
