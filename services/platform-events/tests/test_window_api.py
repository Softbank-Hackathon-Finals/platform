"""HTTP authentication, bounded parsing and the real transactional storage port."""

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from test_window_store import PRODUCER, Fixture, window

from bravo_platform_events import window_api
from bravo_platform_events.window_api import FileProducerCredential, create_window_app

TOKEN = "test-producer-credential-not-a-secret-0001"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
URL = "/v1/observability/windows"


@pytest.fixture
def host(backend, tmp_path):
    fixture = Fixture(backend)
    fixture.bind()
    path = tmp_path / "producer.json"
    path.write_text(json.dumps({"token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                               "identity": asdict(PRODUCER)}))
    app = create_window_app(fixture.intake, resolve_producer=FileProducerCredential(path))
    return fixture, path, app, TestClient(app)


def test_receive_acknowledges_committed_storage_and_rejects_duplicate(host):
    fixture, _, _, client = host
    assert client.get("/readyz").status_code == 200
    response = client.post(URL, json=window(), headers=HEADERS)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert fixture.raw_latest()["received_at"] == response.json()["received_at"]
    assert client.post(URL, json=window(), headers=HEADERS).status_code == 409
    assert client.get("/openapi.json").status_code == 404
    assert client.post("/bindings", json={}).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
    {"Authorization": f"Basic {TOKEN}"},
    [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")]])
def test_wrong_missing_or_ambiguous_credential_never_writes(host, headers):
    fixture, _, _, client = host
    assert client.post(URL, json=window(), headers=headers).status_code == 401
    assert fixture.raw_latest() is None


@pytest.mark.parametrize("body", [b'{"plan_id":1,"plan_id":2}', b'{"x":NaN}',
    b'{"x":1e999}', b'\xff', b'[' * 100 + b']' * 100])
def test_ambiguous_or_unbounded_json_never_writes(host, body):
    fixture, _, _, client = host
    assert client.post(URL, content=body, headers=HEADERS).status_code == 400
    assert fixture.raw_latest() is None


def test_limits_and_encoding_checked_before_persistence(host):
    fixture, _, _, client = host
    assert client.post(URL, content=b"x" * 65537, headers=HEADERS).status_code == 413
    assert client.post(URL, content=b"x", headers=HEADERS | {"Content-Encoding": "gzip"}).status_code == 415
    assert fixture.raw_latest() is None


def test_upstream_rejection_codes_and_long_timestamp(host):
    fixture, _, _, client = host
    assert client.post(URL, json={}, headers=HEADERS).status_code == 422
    assert client.post(URL, json=window() | {"plan_id": "other-plan"}, headers=HEADERS).status_code == 404
    long_time = "2026-11-08T05:31:10." + "0" * 64 + "Z"
    document = window()
    document["window"]["start"] = long_time
    assert client.post(URL, json=document, headers=HEADERS).status_code == 422
    assert fixture.raw_latest() is None


def test_authentication_precedes_reading_request_body(host):
    fixture, _, app, _ = host
    consumed = []

    async def exercise():
        async def body():
            consumed.append(True)
            yield b"not-json"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(URL, content=body())

    assert asyncio.run(exercise()).status_code == 401
    assert consumed == [] and fixture.raw_latest() is None


def test_missing_or_malformed_secret_is_unavailable_without_diagnostics(host):
    fixture, path, _, client = host
    for content in ('{"token_sha256":"private-marker"}', '{"identity":{},"identity":{}}'):
        path.write_text(content)
        response = client.post(URL, json=window(), headers=HEADERS)
        assert response.status_code == 503 and "private-marker" not in response.text
    path.unlink()
    assert client.post(URL, json=window(), headers=HEADERS).status_code == 503
    assert fixture.raw_latest() is None


def test_database_revocation_is_immediate(host):
    fixture, _, _, client = host
    fixture.bind(enabled=False, revision=1)
    assert client.post(URL, json=window(), headers=HEADERS).status_code == 403
    assert fixture.raw_latest() is None


def test_secret_rotation_during_body_upload_rejects_old_bearer(host):
    fixture, path, app, _ = host

    async def exercise():
        async def body():
            yield b" "
            value = json.loads(path.read_text())
            value["token_sha256"] = hashlib.sha256(b"replacement-token").hexdigest()
            path.write_text(json.dumps(value))
            yield json.dumps(window()).encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(URL, content=body(), headers=HEADERS)

    assert asyncio.run(exercise()).status_code == 401
    assert fixture.raw_latest() is None


def test_slow_body_times_out(host, monkeypatch):
    fixture, _, app, _ = host
    monkeypatch.setattr(window_api, "MAX_BODY_SECONDS", 0.01)

    async def exercise():
        async def body():
            await asyncio.sleep(0.1)
            yield b"{}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(URL, content=body(), headers=HEADERS)

    assert asyncio.run(exercise()).status_code == 408
    assert fixture.raw_latest() is None


def test_secret_rotation_while_waiting_for_actual_database_lock(host):
    fixture, path, app, client = host
    started = Event()
    resolver = FileProducerCredential(path)

    def resolve(request):
        result = resolver(request)
        started.set()
        return result

    client = TestClient(create_window_app(fixture.intake, resolve_producer=resolve))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with fixture.backend.store._transaction(write=True, plan_id=PRODUCER.plan_id):
            pending = pool.submit(client.post, URL, json=window(), headers=HEADERS)
            assert started.wait(5)
            if fixture.backend.cluster:
                deadline = time.monotonic() + 3
                with fixture.backend.cluster.connect(fixture.backend.database) as observer:
                    while not observer.execute("SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype='advisory' AND NOT granted").fetchone()[0]:
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
            value = json.loads(path.read_text())
            value["token_sha256"] = hashlib.sha256(b"replacement-token").hexdigest()
            path.write_text(json.dumps(value))
        assert pending.result(timeout=10).status_code == 401
    assert fixture.raw_latest() is None


def test_ambiguous_commit_is_not_retried_or_reported_success(host, monkeypatch):
    fixture, _, _, client = host
    receive = fixture.intake.receive
    calls = []

    def lose_ack(*args, **kwargs):
        calls.append(1)
        receive(*args, **kwargs)
        raise RuntimeError("private-driver-diagnostic")

    monkeypatch.setattr(fixture.intake, "receive", lose_ack)
    response = client.post(URL, json=window(), headers=HEADERS)
    assert response.status_code == 503 and "private-driver" not in response.text
    assert calls == [1] and fixture.raw_latest() is not None
