import ssl

import httpx
import pytest
from starlette.testclient import TestClient

from bravo_console.operator_bridge import OperatorBridge

ORIGIN = "http://127.0.0.1:18123"
# A browser marks this origin's own fetches and EventSource as same-origin.
OWN = {"Sec-Fetch-Site": "same-origin"}


def client(app):
    return TestClient(app, base_url=ORIGIN, headers=OWN)


def case(handler=None, clock=None):
    seen = []

    def read(request):
        seen.append(request)
        return handler(request) if handler else httpx.Response(200, json={"connected": True})

    app = OperatorBridge(
        origin=ORIGIN,
        upstream="https://upstream.example.invalid",
        tls_context=ssl.create_default_context(),
        proof=lambda: "fixture-personal-proof",
        transport=httpx.MockTransport(read),
        clock=clock,
    )
    return app, seen


def login(c, app):
    # the operator opens the one-use URL from the CLI: a top-level navigation with no initiator
    r = c.get(
        "/_operator/" + app.bootstrap, follow_redirects=False, headers={"Sec-Fetch-Site": "none"}
    )
    assert r.status_code == 303
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]


def test_proof_stays_upstream_and_bootstrap_is_one_time():
    app, seen = case()
    with client(app) as c:
        assert c.get("/v1/plans/plan-demo").status_code == 401
        login(c, app)
        assert c.get("/_operator/" + app.bootstrap).status_code == 403
        r = c.get("/v1/plans/plan-demo")
        assert r.status_code == 200 and "fixture-personal-proof" not in r.text
    assert seen[0].headers["authorization"] == "Bearer fixture-personal-proof"
    assert "cookie" not in seen[0].headers


def test_origin_token_substitution_cross_site_and_arbitrary_routes_are_denied():
    app, seen = case()
    with client(app) as c:
        login(c, app)
        assert c.get("/console/", headers={"Authorization": "Bearer other"}).status_code == 403
        assert c.get("/console/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert c.get("/other-service/secret").status_code == 404
        assert c.get("/console/", headers={"Host": "evil.invalid"}).status_code == 403
    assert not seen


WRITES = [
    "/v1/plans/plan-demo/approvals",
    "/v1/plans/plan-demo/cutover",
    "/v1/plans/plan-demo/failback",
    "/v1/plans/plan-demo/delegations",
    "/v1/plans/plan-demo/infra-approvals/approval-1/claims",
    "/v1/plans/plan-demo/iac/plans/iac-1/pull-request",
    "/v1/environments",
]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_write_reaches_the_platform_even_with_console_headers(method):
    app, seen = case()
    with client(app) as c:
        login(c, app)
        for path in WRITES:
            r = c.request(
                method,
                path,
                json={"approval_kind": "infra"},
                headers={"Origin": ORIGIN, "X-Bravo-Console": "1", "Idempotency-Key": "k"},
            )
            assert r.status_code == 403 and r.json()["detail"]["code"] == "operator_bridge_boundary"
    assert not seen


@pytest.mark.parametrize("site", [None, "same-site", "cross-site", "unexpected"])
def test_only_this_origin_or_an_operator_navigation_uses_the_session(site):
    # Another 127.0.0.1 port is same-site and the cookie is not port-scoped.
    app, seen = case()
    with client(app) as c:
        login(c, app)
        if site is None:
            c.headers.pop("Sec-Fetch-Site")
            assert c.get("/v1/plans/plan-demo").status_code == 403
        else:
            r = c.get("/v1/plans/plan-demo", headers={"Sec-Fetch-Site": site})
            assert r.status_code == 403
    assert not seen


def test_a_read_with_a_body_is_refused_and_only_read_headers_are_forwarded():
    app, seen = case()
    with client(app) as c:
        login(c, app)
        assert c.request("GET", "/v1/plans/plan-demo", content=b"{}").status_code == 400
        r = c.get(
            "/v1/plans/plan-demo/events",
            headers={"Origin": ORIGIN, "X-Bravo-Console": "1", "Last-Event-ID": "plan-demo:3"},
        )
        assert r.status_code == 200
    assert len(seen) == 1 and seen[0].method == "GET" and seen[0].content == b""
    assert seen[0].headers["last-event-id"] == "plan-demo:3"
    assert "x-bravo-console" not in seen[0].headers and "origin" not in seen[0].headers


def test_expired_local_session_does_not_mint_a_cloud_proof():
    now = [1000]
    app, seen = case(clock=lambda: now[0])
    with client(app) as c:
        login(c, app)
        now[0] += 900
        assert c.get("/console/").status_code == 401
    assert not seen


@pytest.mark.parametrize(
    "change",
    [
        {"upstream": "http://untrusted.invalid"},
        {"origin": "http://0.0.0.0:18123"},
        {"tls_context": False},
    ],
)
def test_unverified_transports_fail_before_startup(change):
    args = dict(
        origin=ORIGIN,
        upstream="https://example.invalid",
        tls_context=ssl.create_default_context(),
        proof=lambda: "fixture",
    )
    args.update(change)
    with pytest.raises(ValueError):
        OperatorBridge(**args)


def test_upstream_redirect_is_not_followed():
    app, seen = case(lambda r: httpx.Response(302, headers={"Location": "https://evil.invalid"}))
    with client(app) as c:
        login(c, app)
        assert c.get("/console/").status_code == 502
    assert len(seen) == 1


def test_invalid_non_ascii_cookie_is_rejected_without_forwarding():
    app, seen = case()
    with client(app) as c:
        response = c.get("/console/", headers=[(b"cookie", b"sova-operator=\xff")])
        assert response.status_code == 401
    assert not seen


def test_server_name_is_fixed_by_operator_not_browser_headers():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    app = OperatorBridge(
        origin=ORIGIN,
        upstream="https://127.0.0.1:16494",
        tls_server_name="host.platform-state.svc",
        tls_context=ssl.create_default_context(),
        proof=lambda: "fixture-proof",
        transport=httpx.MockTransport(upstream),
    )
    with client(app) as c:
        login(c, app)
        assert (
            c.get("/console/", headers={"X-Forwarded-Host": "untrusted.invalid"}).status_code == 200
        )
    assert seen[0].extensions["sni_hostname"] == "host.platform-state.svc"
    assert "x-forwarded-host" not in seen[0].headers
