"""Actual T10 plan authorization; synthetic T37 metric samples are explicitly labelled."""

import ssl
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console import ConsoleApp
from bravo_console.observations import PlanObservations, T37Client


class Samples:
    def __init__(self):
        self.calls = []
        self.mutate = lambda: None
        self.body_change = lambda body: body

    async def http(self, env):
        self.calls.append(env)
        self.mutate()
        return self.body_change(
            dict(
                environment=env,
                window="1m",
                state="OK",
                queried_at=datetime.now(UTC).isoformat(),
                last_received_seconds_ago=1,
                requests_per_second=10,
                error_ratio=0.1,
                p95_seconds=0.2,
            )
        )


@pytest.fixture
def bridged(tmp_path):
    wrapped, store, auth, _ = setup(tmp_path / "obs.db")
    samples, permission = Samples(), {"allowed": True}
    reader = PlanObservations(
        wrapped.api,
        samples,
        environments={"env-source": "source", "env-target": "target"},
        authorize=lambda *_: permission["allowed"],
    )
    app = ConsoleApp(wrapped.api, public_origin=ORIGIN, observations=reader)
    return app, samples, permission, auth, store


def test_registered_plan_and_extra_observation_grant_required(bridged):
    app, samples, permission, auth, _ = bridged
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/observations").status_code == 403
        assert samples.calls == []
        c.cookies.set("fixture-session", "test-only")
        permission["allowed"] = False
        assert c.get("/v1/plans/plan-demo/observations").status_code == 403
        assert samples.calls == []
        permission["allowed"] = True
        response = c.get("/v1/plans/plan-demo/observations")
        assert response.status_code == 200
        assert response.json()["for_gate_decisions"] is False
        assert samples.calls == ["source", "target"]
        assert response.json()["environments"][1]["error_ratio"] == 0.1
        auth["allowed"] = False
        assert c.get("/v1/plans/plan-demo/observations").status_code == 403


def test_revocation_during_external_query_discards_results(bridged):
    app, samples, permission, _, _ = bridged
    samples.mutate = lambda: permission.update(allowed=False)
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        response = c.get("/v1/plans/plan-demo/observations")
        assert response.status_code == 403 and "requests_per_second" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "future",
        "wrong-env",
        "nan",
        "boolean",
        "old-heartbeat",
        "state-unknown",
        "ratio-range",
        "missing-count",
    ],
)
def test_stale_invalid_or_misbound_sample_never_becomes_zero_or_ok(bridged, change):
    app, samples, *_ = bridged

    def bad(body):
        if change == "stale":
            body["queried_at"] = (datetime.now(UTC) - timedelta(seconds=11)).isoformat()
        elif change == "future":
            body["queried_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
        elif change == "wrong-env":
            body["environment"] = "another-tenant"
        elif change == "nan":
            body["p95_seconds"] = float("nan")
        elif change == "boolean":
            body["requests_per_second"] = True
        elif change == "old-heartbeat":
            body["last_received_seconds_ago"] = 31
        elif change == "state-unknown":
            body["state"] = "UNOBSERVABLE"
        elif change == "ratio-range":
            body["error_ratio"] = 2
        elif change == "missing-count":
            del body["requests_per_second"]
        return body

    samples.body_change = bad
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        rows = c.get("/v1/plans/plan-demo/observations").json()["environments"]
        assert all(
            r["state"] == "UNOBSERVABLE" and r["error_ratio"] is None and r["p95_seconds"] is None
            for r in rows
        )


def test_t37_connection_configuration_rejects_insecure_external_route_and_tls():
    with pytest.raises(ValueError):
        T37Client("http://remote.invalid", "x" * 32)
    context = ssl.create_default_context()
    context.check_hostname = False
    with pytest.raises(ValueError, match="verified_tls"):
        T37Client("https://remote.invalid", "x" * 32, tls=context)
    with pytest.raises(ValueError):
        T37Client("https://remote.invalid/?q=a", "x" * 32)


@pytest.mark.parametrize(
    "origin",
    ["https://remote.invalid\n", "https://remote.invalid\\other", " https://remote.invalid"],
)
def test_ambiguous_upstream_origin_rejected(origin):
    with pytest.raises(ValueError):
        T37Client(origin, "x" * 32)


def test_control_character_token_and_wrong_tls_type_rejected():
    with pytest.raises(ValueError):
        T37Client("https://remote.invalid", "x" * 32 + "\x00")
    with pytest.raises(ValueError):
        T37Client("https://remote.invalid", "x" * 32, tls=False)


def test_real_loopback_t37_transport_bounds_body_and_does_not_follow_redirect():
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = "fixture-observation-credential-00000000"
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            seen.append(self.path)
            assert self.headers["Accept-Encoding"] == "identity"
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(401)
                return
            if "/redirect/" in self.path:
                self.send_response(302)
                self.send_header("Location", "/should-not-follow")
                self.end_headers()
                return
            body = json.dumps(
                {"private": "x" * 70000} if "/large/" in self.path else {"environment": "source"}
            ).encode()
            variants = {
                "duplicate": b'{"environment":"wrong","environment":"source"}',
                "nan": b'{"requests_per_second":NaN}',
                "overflow": b'{"requests_per_second":1e999}',
                "utf16": '{"environment":"source"}'.encode("utf-16"),
                "deep": b'{"a":' + b"[" * 40 + b"0" + b"]" * 40 + b"}",
                "surrogate": b'{"environment":"\\ud800"}',
            }
            for key, value in variants.items():
                if f"/{key}/" in self.path:
                    body = value
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if "/compressed/" in self.path:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def check():
        from bravo_console.observations import ObservationError

        transport = T37Client(f"http://127.0.0.1:{server.server_port}", token)
        try:
            assert await transport.http("source") == {"environment": "source"}
            for env in (
                "redirect",
                "large",
                "duplicate",
                "nan",
                "overflow",
                "utf16",
                "deep",
                "surrogate",
                "compressed",
            ):
                with pytest.raises(ObservationError, match="observation_unavailable"):
                    await transport.http(env)
            assert not any("should-not-follow" in path for path in seen)
        finally:
            await transport.aclose()

    try:
        asyncio.run(check())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_drip_response_has_whole_request_deadline():
    import asyncio
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from bravo_console.observations import ObservationError

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "200")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)  # Faster than the per-read timeout.
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def check():
        client = T37Client(f"http://127.0.0.1:{server.server_port}", "x" * 40)
        started = time.monotonic()
        try:
            with pytest.raises(ObservationError, match="observation_unavailable"):
                await client.http("source")
            assert 4.5 <= time.monotonic() - started < 8
        finally:
            await client.aclose()

    try:
        asyncio.run(check())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
