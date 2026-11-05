"""Actual T10 authorization; synthetic T33 samples and real T33 projection contract."""

import asyncio
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console import ConsoleApp
from bravo_console.observations import ObservationError
from bravo_console.security import CATEGORIES, PlanSecurity, T33Client, project_summary


def sample(env, now=None):
    return {
        "generated_time": now if now is not None else int(time.time() * 1000),
        "categories": list(CATEGORIES),
        "environments": [
            {
                "environment_id": env,
                "cells": {
                    key: {
                        "state": "OK",
                        "counts": {"4": 2, "0": 1},
                        "sources": ["aws_security_hub"],
                    }
                    for key in CATEGORIES
                },
            }
        ],
    }


class Samples:
    def __init__(self):
        self.calls = []
        self.mutate = lambda: None
        self.transform = lambda body: body

    async def summary(self, env):
        self.calls.append(env)
        self.mutate()
        return self.transform(sample(env))


@pytest.fixture
def bridged(tmp_path):
    wrapped, store, auth, _ = setup(tmp_path / "security.db")
    upstream, permission = Samples(), {"allowed": True}
    reader = PlanSecurity(
        wrapped.api,
        upstream,
        environments={"env-source": "source", "env-target": "target"},
        authorize=lambda *_: permission["allowed"],
    )
    return (
        ConsoleApp(wrapped.api, public_origin=ORIGIN, security=reader),
        upstream,
        permission,
        auth,
        store,
    )


def test_plan_and_environment_permission_before_and_after_read(bridged):
    app, upstream, permission, auth, _ = bridged
    with TestClient(app, base_url=ORIGIN) as c:
        assert c.get("/v1/plans/plan-demo/security").status_code == 403
        assert not upstream.calls
        c.cookies.set("fixture-session", "test-only")
        permission["allowed"] = False
        assert c.get("/v1/plans/plan-demo/security").status_code == 403
        assert not upstream.calls
        permission["allowed"] = True
        response = c.get("/v1/plans/plan-demo/security")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        value = response.json()
        assert value["for_gate_decisions"] is False
        assert value["environments"][0]["cells"]["IAM"]["counts"] == {"0": 1, "4": 2}
        assert upstream.calls == ["source", "target"]
        assert "aws_security_hub" not in response.text  # producer metadata is not forwarded
        upstream.mutate = lambda: permission.update(allowed=False)
        assert c.get("/v1/plans/plan-demo/security").status_code == 403
        permission["allowed"] = True
        upstream.mutate = lambda: auth.update(allowed=False)
        assert c.get("/v1/plans/plan-demo/security").status_code == 403


def test_changed_plan_binding_rejected_after_fetch(bridged):
    app, upstream, _, _, store = bridged

    def mutate():
        with store._transaction(write=True, plan_id="plan-demo") as db:
            db.execute(
                "UPDATE plans SET spec_json=json_set(spec_json,'$.workload_id','other-workload') WHERE plan_id='plan-demo'"
            )

    upstream.mutate = mutate
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        response = c.get("/v1/plans/plan-demo/security")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "security_scope_changed"
        assert "counts" not in response.text


def test_query_post_unknown_plan_and_unconfigured_rejected(bridged):
    app, upstream, *_ = bridged
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        assert c.get("/v1/plans/plan-demo/security?environment_id=other").status_code == 400
        assert c.post("/v1/plans/plan-demo/security").status_code == 405
        assert c.get("/v1/plans/unknown/security").status_code == 403
        assert not upstream.calls
        app.security = None
        assert c.get("/v1/plans/plan-demo/security").status_code == 503


@pytest.mark.parametrize("state", ["UNKNOWN", "STALE", "NONE"])
def test_unavailable_states_never_forward_historic_counts(state):
    body = sample("source", 10000)
    for cell in body["environments"][0]["cells"].values():
        cell["state"] = state
        if state == "NONE":
            cell.update(counts={}, sources=[])
    projected = project_summary(body, "source", 10001)
    assert all(c == {"state": state, "counts": None} for c in projected.values())


@pytest.mark.parametrize(
    "case",
    [
        "old",
        "future",
        "wrong-env",
        "duplicate-env",
        "extra-env",
        "missing-cell",
        "extra-cell",
        "wrong-categories",
        "bad-state",
        "bool-count",
        "negative-count",
        "huge-count",
        "bad-severity",
        "null-count",
        "no-source",
        "duplicate-source",
        "none-with-count",
        "non-int-time",
    ],
)
def test_invalid_upstream_is_unknown_not_zero(case):
    body = sample("source", 100000)
    row = body["environments"][0]
    cell = row["cells"]["IAM"]
    if case == "old":
        body["generated_time"] -= 10001
    if case == "future":
        body["generated_time"] += 1
    if case == "wrong-env":
        row["environment_id"] = "another-tenant"
    if case == "duplicate-env":
        body["environments"].append(copy.deepcopy(row))
    if case == "extra-env":
        body["environments"].append({"environment_id": "secret"})
    if case == "missing-cell":
        del row["cells"]["AUDIT"]
    if case == "extra-cell":
        row["cells"]["invented"] = cell
    if case == "wrong-categories":
        body["categories"] = ["IAM"]
    if case == "bad-state":
        cell["state"] = "PASS"
    if case == "bool-count":
        cell["counts"] = {"1": True}
    if case == "negative-count":
        cell["counts"] = {"1": -1}
    if case == "huge-count":
        cell["counts"] = {"1": 2**53}
    if case == "bad-severity":
        cell["counts"] = {"7": 1}
    if case == "null-count":
        cell["counts"] = None
    if case == "no-source":
        cell["sources"] = []
    if case == "duplicate-source":
        cell["sources"] *= 2
    if case == "none-with-count":
        cell["state"] = "NONE"
    if case == "non-int-time":
        body["generated_time"] = True
    assert all(
        c == {"state": "UNKNOWN", "counts": None}
        for c in project_summary(body, "source", 100000).values()
    )


def test_one_failed_environment_keeps_other_qualified_summary(bridged):
    app, upstream, *_ = bridged

    def transform(body):
        if body["environments"][0]["environment_id"] == "target":
            raise TimeoutError()
        body["secret"] = "never-forward-me"
        body["environments"][0]["cells"]["IAM"]["raw"] = "never-forward-me"
        return body

    upstream.transform = transform
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        response = c.get("/v1/plans/plan-demo/security")
        rows = response.json()["environments"]
        assert rows[0]["cells"]["IAM"]["state"] == "OK"
        assert rows[1]["cells"]["IAM"] == {"state": "UNKNOWN", "counts": None}
        assert "never-forward-me" not in response.text


def test_actual_t33_view_contract_without_changing_collection_semantics(tmp_path, monkeypatch):
    # Import the current producer, not a rewritten fixture of its summary logic.
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "services/security-findings/src"))
    from bravo_findings.rules import Rules
    from bravo_findings.store import Store
    from bravo_findings.view import View

    store = Store(str(tmp_path / "producer.db"))
    try:
        store.register_source("aws_security_hub", "source")
        store.record_success("aws_security_hub", "source", 100000)
        view = View(store, Rules())
        assert project_summary(view.summary(100001, {"source"}), "source", 100002)["IAM"] == {
            "state": "OK",
            "counts": {},
        }
        store.record_failure("aws_security_hub", "source", 100003, "ACCESS_DENIED")
        assert project_summary(view.summary(100004, {"source"}), "source", 100005)["IAM"] == {
            "state": "UNKNOWN",
            "counts": None,
        }
        assert project_summary(view.summary(100004, {"target"}), "target", 100005)["IAM"] == {
            "state": "UNKNOWN",
            "counts": None,
        }
    finally:
        store.close()


def test_real_t33_http_query_bounds_and_redirects():
    token = "test-only-security-reader-000000000"
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            seen.append(self.path)
            assert self.headers["Authorization"] == "Bearer " + token
            assert self.headers["Accept-Encoding"] == "identity"
            if "redirect" in self.path:
                self.send_response(302)
                self.send_header("Location", "/leak")
                self.end_headers()
                return
            body = json.dumps(sample("source")).encode()
            if "large" in self.path:
                body = b"x" * 65537
            if "duplicate" in self.path:
                body = b'{"a":1,"a":2}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise():
        client = T33Client(f"http://127.0.0.1:{server.server_port}", token)
        try:
            assert (await client.summary("source"))["categories"] == list(CATEGORIES)
            for env in ("redirect", "large", "duplicate", "bad&env=x"):
                with pytest.raises(ObservationError):
                    await client.summary(env)
        finally:
            await client.aclose()

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert seen == [
        "/v1/security/summary?environment_id=" + e
        for e in ("source", "redirect", "large", "duplicate")
    ]


@pytest.mark.parametrize(
    "mapping", [{}, {"one": "same", "two": "same"}, {"one": "bad/query"}, {1: "source"}]
)
def test_registration_mapping_fails_closed(mapping):
    with pytest.raises(ValueError):
        PlanSecurity(None, None, environments=mapping, authorize=lambda *_: True)


def test_real_t33_api_service_scope_revocation_and_real_t10_view(tmp_path, monkeypatch):
    import hashlib

    import httpx

    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "services/security-findings/src"))
    from bravo_findings.api import create_app
    from bravo_findings.rules import Rules
    from bravo_findings.store import Store

    now = int(time.time() * 1000)
    token = "test-only-t33-reader-" + "x" * 40
    credentials = [
        {
            "credential_id": "fixture-reader",
            "subject": "console-fixture",
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "environments": ["source"],
            "permissions": ["read"],
            "sources": [],
            "not_before": now - 1000,
            "expires_at": now + 60000,
        }
    ]
    producer_store = Store(str(tmp_path / "actual-t33.db"))
    for env in ("source", "target"):
        producer_store.register_source("aws_security_hub", env)
        producer_store.record_success("aws_security_hub", env, now)
    producer = create_app(producer_store, Rules(), credentials=credentials)
    # In-process HTTP transport, actual T33 routing/auth/View and T21 parsing.
    transport = T33Client("http://127.0.0.1", token)
    asyncio.run(transport.aclose())
    transport.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=producer),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
    )
    wrapped, _, _, _ = setup(tmp_path / "actual-t10.db")
    wrapped.security = PlanSecurity(
        wrapped.api,
        transport,
        environments={"env-source": "source", "env-target": "target"},
        authorize=lambda *_: True,
    )
    try:
        with TestClient(
            wrapped, base_url=ORIGIN, cookies={"fixture-session": "test-only"}
        ) as browser:
            result = browser.get("/v1/plans/plan-demo/security")
            rows = result.json()["environments"]
            assert rows[0]["cells"]["IAM"] == {"state": "OK", "counts": {}}
            assert rows[1]["cells"]["IAM"] == {"state": "UNKNOWN", "counts": None}
            assert token not in result.text
            credentials[0]["revoked"] = True
            revoked = browser.get("/v1/plans/plan-demo/security").json()
            assert all(r["cells"]["IAM"]["state"] == "UNKNOWN" for r in revoked["environments"])
    finally:
        asyncio.run(transport.aclose())
        producer_store.close()
