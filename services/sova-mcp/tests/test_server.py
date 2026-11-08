"""Wire-level checks: real uvicorn, stateless Streamable HTTP, the T10 SQLite store and Registry."""

import json
import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.registration import Registry
from bravo_platform_events.registration_probe import EnvironmentRef, SourceRelease
from bravo_platform_events.registration_sso import SsoRegistrationPrincipal

from sova_mcp.backend import SovaBackend, Upstream
from sova_mcp.server import Confirmations, create_app

TENANT = "team"
APP = dict(name="chat", repo_url="https://example.invalid/chat", app_path="app", iac_path="compose.yaml",
           source_environment_id="env-src", namespace="app-src")


class Probe:
    def inspect(self, env, release=None):
        result = dict(namespace=env.namespace, database="chat", database_identity=env.database_uid,
                      collector="scoped-reader", warnings=[], read_access="verified")
        if release:
            result.update(rollout_identity="rs", source_sha="a" * 40, image="b" * 64, service_account="chat",
                          repo_deploy_match=True, resources=[{"kind": "Cluster", "name": "chat"}])
        return result


def principal(subject, tenant=TENANT):
    t = int(time.time())
    return SsoRegistrationPrincipal(subject, tenant, t - 1, t + 300, "s-" + subject)


def grant(store, plan_id, subject, permissions=("read",), revision=0):
    """The T10 administrator's per-plan grant (set_permissions); the MCP caller is checked against it."""
    t = int(time.time())
    who = OperatorPrincipal(subject, TENANT, t - 1, t + 300, t - 1, "grant-" + subject)
    OperatorAccess(store).set_permissions(
        plan_id, who, permissions=list(permissions),
        valid_after=datetime.fromtimestamp(t - 60, UTC).isoformat(),
        expires_at=datetime.fromtimestamp(t + 3600, UTC).isoformat(), expected_revision=revision,
        administration_ref="fixture-admin", command_key=f"grant-{subject}-{revision}")


@pytest.fixture
def server(tmp_path):
    store = PlanStore(tmp_path / "plans.db")
    src = EnvironmentRef(TENANT, "env-src", "aws", "app-src", "db", "src-id", "https://source.invalid",
                         tmp_path / "ca", lambda: "test-only")
    dst = replace(src, environment_id="env-dst", kind="onprem", namespace="app-dst", database_uid="dst-id")
    release = SourceRelease(TENANT, "env-src", APP["repo_url"], tmp_path, "app", "compose.yaml", "a" * 40,
                            "chat", "chat", "repo/chat@sha256:" + "b" * 64, ("sha256:" + "b" * 64,), "chat", "rs-uid")
    registry = Registry(store, environments={(TENANT, "source-reader"): src, (TENANT, "target-reader"): dst},
                        releases=[release], can_register=lambda _a: True, probe=Probe(),
                        evaluation_profiles=[dict(tenant_id=TENANT, profile_id="full", profile_version="1")])
    seen = []

    def upstream(request):
        seen.append((request.url.path, dict(request.url.params), request.headers.get("authorization")))
        return httpx.Response(200, json={"rps": 12.5, "note": "x" * 5000})

    (tmp_path / "obs-token").write_text("host-own-token")
    obs = Upstream("https://obs.invalid", tmp_path / "obs-token", httpx.MockTransport(upstream))
    backend = SovaBackend(store, registry, "https://console.invalid/console/",
                          {TENANT: {"env-src": {"observability": "source", "security": "src-sec"}}}, obs, obs)
    t = int(time.time())
    actors = {"Bearer alice": principal("alice"), "Bearer mallory": principal("mallory", "other"),
              "Bearer bob": principal("bob"),  # same tenant, no plan grant
              # a short proof: a stream that is not closed by a revoked grant still ends by itself
              "Bearer carol": SsoRegistrationPrincipal("carol", TENANT, t - 1, t + 8, "s-carol"),
              "Bearer brief": SsoRegistrationPrincipal("brief", TENANT, t - 1, t + 2, "s-brief")}
    app = create_app(backend, lambda r: actors.get(r.headers.get("authorization")),
                     resource_url="http://127.0.0.1/mcp", allowed_hosts=["127.0.0.1:*"])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", seen, store
    srv.should_exit = True
    thread.join(5)


def rpc(base, method, params=None, token="alice", query=""):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return httpx.post(f"{base}/mcp{query}", json=body, headers=headers, timeout=10)


def call(base, name, args=None, **kw):
    response = rpc(base, "tools/call", {"name": name, "arguments": args or {}}, **kw)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result, result.get("structuredContent")


def test_unauthenticated_gets_resource_metadata_challenge(server):
    base, _, _ = server
    response = rpc(base, "tools/list", token=None)
    assert response.status_code == 401
    assert 'resource_metadata="http://127.0.0.1/.well-known/oauth-protected-resource/mcp"' in response.headers["www-authenticate"]
    prm = httpx.get(base + "/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "http://127.0.0.1/mcp" and "sova:read" in prm["scopes_supported"]


def test_standalone_get_stream_is_refused(server):
    base, _, _ = server
    response = httpx.get(base + "/mcp", headers={"Authorization": "Bearer alice", "Accept": "text/event-stream"}, timeout=5)
    assert response.status_code == 405 and response.headers["allow"] == "POST"


def test_initialize_carries_onboarding_instructions(server):
    base, _, _ = server
    result = rpc(base, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert "sova_get_context" in result["instructions"] and result["serverInfo"]["name"] == "sova"


def test_read_only_hides_and_refuses_write_tools(server):
    base, _, _ = server
    names = {t["name"] for t in rpc(base, "tools/list").json()["result"]["tools"]}
    assert "sova_get_context" in names and "sova_register_environment" not in names
    writable = {t["name"] for t in rpc(base, "tools/list", query="?read_only=false").json()["result"]["tools"]}
    assert {"sova_register_environment", "sova_create_plan"} <= writable
    result, _ = call(base, "sova_register_environment",
                     {"name": "s", "kind": "aws", "role": "source", "connection_ref": "source-reader"})
    assert result["isError"] and "read_only_connection" in result["content"][0]["text"]


def test_guided_registration_needs_matching_confirmation_then_plan_flows(server):
    base, _, store = server
    q = "?read_only=false"
    env = {"name": "source", "kind": "aws", "role": "source", "connection_ref": "source-reader"}
    _, preview = call(base, "sova_register_environment", env, query=q)
    assert preview["status"] == "NEEDS_CONFIRMATION" and preview["data"]["input"]["roles"] == ["source"]
    # Same consent cannot be reused for a different input or by another person.
    tampered, _ = call(base, "sova_register_environment", env | {"name": "other", "confirmation": preview["confirmation"]}, query=q)
    assert tampered["isError"] and "confirmation_invalid" in tampered["content"][0]["text"]
    _, done = call(base, "sova_register_environment", env | {"confirmation": preview["confirmation"]}, query=q)
    assert done["status"] == "ACCEPTED" and done["data"]["environment_id"] == "env-src"
    _, again = call(base, "sova_register_environment", env | {"confirmation": preview["confirmation"]}, query=q)
    assert again["status"] == "OK"  # replay, not a second registration

    dst = {"name": "target", "kind": "onprem", "role": "target", "connection_ref": "target-reader"}
    _, p = call(base, "sova_register_environment", dst, query=q)
    call(base, "sova_register_environment", dst | {"confirmation": p["confirmation"]}, query=q)
    _, p = call(base, "sova_register_workload", APP, query=q)
    _, app = call(base, "sova_register_workload", APP | {"confirmation": p["confirmation"]}, query=q)
    plan_in = {"workload_id": app["data"]["workload_id"], "target_environment_id": "env-dst",
               "profile_id": "full", "profile_version": "1"}
    _, p = call(base, "sova_create_plan", plan_in, query=q)
    _, plan = call(base, "sova_create_plan", plan_in | {"confirmation": p["confirmation"]}, query=q)
    plan_id = plan["data"]["plan_id"]
    # Creating a plan grants nothing: like the T10 API, reading it needs the administrator's per-plan grant.
    denied, _ = call(base, "sova_get_plan", {"plan_id": plan_id})
    assert denied["isError"] is True
    grant(store, plan_id, "alice")

    _, ctx = call(base, "sova_get_context")
    assert len(ctx["data"]["environments"]) == 2 and ctx["data"]["plans"][0]["plan_id"] == plan_id
    assert ctx["data"]["connections"] == ["source-reader", "target-reader"]
    assert ctx["data"]["releases"] == [{"environment_id": "env-src", "repo_url": APP["repo_url"],
                                        "app_path": "app", "iac_path": "compose.yaml"}]
    _, other = call(base, "sova_get_context", token="mallory")
    assert other["data"]["connections"] == [] and other["data"]["releases"] == []
    _, steps = call(base, "sova_next_steps", {"plan_id": plan_id})
    assert steps["data"]["state"] == "COLLECT" and steps["next_actions"][0]["tool"] == "sova_get_progress"
    _, progress = call(base, "sova_get_progress", {"plan_id": plan_id})
    assert progress["status"] == "RUNNING" and progress["cursor"] == f"{plan_id}:1"
    _, later = call(base, "sova_get_progress", {"plan_id": plan_id, "since": progress["cursor"]})
    assert later["data"]["events"] == [] and later["cursor"] == progress["cursor"]
    _, compare = call(base, "sova_compare", {"plan_id": plan_id})
    assert compare["status"] == "UNKNOWN"
    store.append(plan_id=plan_id, run_id="gate-run", event_type="gate.completed", producer_key="gate-1",
                 occurred_at=datetime.now(UTC).isoformat(),
                 data={"gate_id": "gate-1", "evidence_ref": "ev-1", "decision": "BLOCK",
                       "context_digest": "c" * 64})
    _, compare = call(base, "sova_compare", {"plan_id": plan_id})
    assert compare["status"] == "FAILED" and "BLOCK" in compare["summary"]
    _, approval = call(base, "sova_request_approval", {"plan_id": plan_id, "kind": "cutover"})
    assert approval["status"] == "BLOCKED" and approval["console_url"].endswith(f"plan={plan_id}&approve=cutover")

    # Another tenant cannot see it; a plan-scoped connection cannot reach other plans.
    denied, _ = call(base, "sova_get_plan", {"plan_id": plan_id}, token="mallory")
    assert denied["isError"] and "forbidden" in denied["content"][0]["text"]
    scoped, _ = call(base, "sova_get_plan", {"plan_id": plan_id}, query="?plan=another-plan")
    assert scoped["isError"] and "plan_scoped_connection" in scoped["content"][0]["text"]
    resource = rpc(base, "resources/read", {"uri": f"sova://plans/{plan_id}"}).json()["result"]
    assert json.loads(resource["contents"][0]["text"])["state"] == "COLLECT"


def test_observability_uses_host_credential_and_tenant_mapping(server):
    base, seen, _ = server
    _, obs = call(base, "sova_get_observability", {"environment_id": "env-src", "view": "http"})
    assert obs["data"]["rps"] == 12.5
    assert seen[-1][0] == "/v1/observability/environments/source/http"
    assert seen[-1][2] == "Bearer host-own-token"  # never the caller's token
    _, sec = call(base, "sova_get_security", {"environment_id": "env-src", "view": "findings", "min_severity": 3})
    assert seen[-1][1]["environment_id"] == "src-sec" and len(sec["data"]["note"]) == 4001  # clipped
    unmapped, _ = call(base, "sova_get_observability", {"environment_id": "env-src"}, token="mallory")
    assert unmapped["isError"] and "environment_not_connected" in unmapped["content"][0]["text"]


def test_onboarding_prompt_and_guide_resources(server):
    base, _, _ = server
    prompts = {p["name"] for p in rpc(base, "prompts/list").json()["result"]["prompts"]}
    assert {"sova-onboarding", "sova-migrate", "sova-status", "sova-failback"} <= prompts
    flow = rpc(base, "resources/read", {"uri": "sova://guide/flow"}).json()["result"]["contents"][0]["text"]
    assert len(json.loads(flow)) == 8


def test_confirmation_expires():
    clock = [1000.0]
    c = Confirmations(b"k" * 32, clock=lambda: clock[0])
    token = c.issue("alice", "t", {"a": 1})
    assert c.check(token, "alice", "t", {"a": 1}).startswith("mcp-")
    clock[0] += 301
    with pytest.raises(Exception, match="confirmation_invalid"):
        c.check(token, "alice", "t", {"a": 1})
    assert datetime.now(UTC)


def test_plan_events_stream_t10_sse_with_the_same_access_rules(server):
    base, _, store = server
    store.register_plan(PlanSpec("plan-sse", TENANT, "workload-x", "env-src", "env-dst", "a" * 64, "b" * 64),
                        run_id="fixture", occurred_at=datetime.now(UTC).isoformat())
    grant(store, "plan-sse", "alice")
    grant(store, "plan-sse", "brief")
    url = base + "/v1/plans/plan-sse/events"
    alice = {"Authorization": "Bearer alice"}
    assert httpx.get(url).status_code == 401
    assert httpx.get(url, headers={"Authorization": "Bearer mallory"}).json()["detail"]["code"] == "forbidden"
    assert httpx.get(base + "/v1/plans/plan-none/events", headers=alice).json()["detail"]["code"] == "forbidden"
    assert httpx.get(url + "?plan=plan-other", headers=alice).status_code == 403
    assert httpx.post(url, headers=alice).status_code == 404
    assert httpx.get(url, headers=alice | {"Last-Event-ID": "bogus"}).status_code == 400

    # The stream ends by itself when the proof expires; the console reconnects with a new one.
    with httpx.stream("GET", url, headers={"Authorization": "Bearer brief"}, timeout=10) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "id: plan-sse:1\n" in body and '"plan_id":"plan-sse"' in body

    # Resume after the cursor, then follow a new event live.
    with httpx.stream("GET", url, headers=alice | {"Last-Event-ID": "plan-sse:1"}, timeout=10) as r:
        lines = r.iter_lines()
        assert next(line for line in lines if line.startswith(":")) == ": keep-alive"
        store.append(plan_id="plan-sse", run_id="gate-run", event_type="gate.completed", producer_key="gate-1",
                     occurred_at=datetime.now(UTC).isoformat(),
                     data={"gate_id": "gate-1", "evidence_ref": "ev-1", "decision": "PASS", "context_digest": "c" * 64})
        assert next(line for line in lines if line.startswith("id:")) == "id: plan-sse:2"


def test_rejected_arguments_name_fields_without_echoing_values(server):
    base, _, _ = server
    pasted = "https://github.com/Softbank-Hackathon-Finals/demo-app"
    bad = {"name": "src", "kind": "aws", "role": "source", "connection_ref": pasted}
    result, _ = call(base, "sova_register_environment", bad, query="?read_only=false")
    text = result["content"][0]["text"]
    assert result["isError"] and "github" not in text and "input_value" not in text
    body = json.loads(text)
    assert body["detail"] == {"code": "invalid_input", "fields": ["connection_ref"]} and "URL" in body["hint"]


def test_registry_refusals_come_with_a_hint(server):
    base, _, _ = server
    q = "?read_only=false"
    env = {"name": "src", "kind": "aws", "role": "source", "connection_ref": "no-such-ref"}
    _, preview = call(base, "sova_register_environment", env, query=q)
    result, _ = call(base, "sova_register_environment", env | {"confirmation": preview["confirmation"]}, query=q)
    body = json.loads(result["content"][0]["text"][result["content"][0]["text"].index("{"):])
    assert result["isError"] and body["hint"] and body["detail"]["code"] in (
        "registration_reference_unavailable", "invalid_environment_reference")


def test_same_tenant_without_a_plan_grant_reads_nothing_and_a_revoked_grant_ends_the_stream(server):
    # ADR-0040: MCP clients get the same per-plan permission check as the operator API and console (#987 review).
    base, _, store = server
    store.register_plan(PlanSpec("plan-grant", TENANT, "workload-x", "env-src", "env-dst", "a" * 64, "b" * 64),
                        run_id="fixture", occurred_at=datetime.now(UTC).isoformat())
    grant(store, "plan-grant", "alice")
    bob = {"Authorization": "Bearer bob"}
    url = base + "/v1/plans/plan-grant/events"
    with httpx.stream("GET", url, headers=bob) as r:  # a stream would hang here; the refusal comes first
        assert r.status_code == 403 and json.loads(r.read())["detail"]["code"] == "forbidden"
    denied, _ = call(base, "sova_get_plan", {"plan_id": "plan-grant"}, token="bob")
    assert denied["isError"] is True
    _, ctx = call(base, "sova_get_context", token="bob")
    assert "plan-grant" not in [p["plan_id"] for p in ctx["data"]["plans"]]
    _, ctx = call(base, "sova_get_context")
    assert "plan-grant" in [p["plan_id"] for p in ctx["data"]["plans"]]

    grant(store, "plan-grant", "carol")
    with httpx.stream("GET", url, headers={"Authorization": "Bearer carol"}, timeout=15) as r:
        lines = r.iter_lines()
        assert next(line for line in lines if line.startswith("id:")) == "id: plan-grant:1"
        grant(store, "plan-grant", "carol", permissions=(), revision=1)  # the administrator revokes read
        store.append(plan_id="plan-grant", run_id="gate-run", event_type="gate.completed", producer_key="gate-1",
                     occurred_at=datetime.now(UTC).isoformat(),
                     data={"gate_id": "gate-1", "evidence_ref": "ev-1", "decision": "PASS", "context_digest": "c" * 64})
        rest = list(lines)  # ends at the next read (or, without the grant check, only when the proof expires)
    assert "id: plan-grant:2" not in rest
