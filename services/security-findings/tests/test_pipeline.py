"""수집 상태 · 한 화면 · 밀어 넣기 API · 끌어오기 수집기 · 온프렘 에이전트."""

import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber
from conftest import READ_AUTH, WRITE_AUTH
from fastapi.testclient import TestClient

from bravo_findings import Normalizer, Rules
from bravo_findings.agent import collect, policy_report_results
from bravo_findings.api import create_app
from bravo_findings.collectors import SccSource, SecurityHubSource, run_once
from bravo_findings.ingest import Ingestor
from bravo_findings.rules import rules_dir
from bravo_findings.store import Store
from bravo_findings.view import View

EX = rules_dir() / "examples"
MIN = 60_000
T0 = 1_793_786_400_000  # 2026-11-04T10:00:00Z (after all source fixtures)


def raw(name):
    return json.loads((EX / f"{name}.raw.json").read_text(encoding="utf-8"))


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def rules():
    return Rules()


@pytest.fixture
def env(rules, service_credentials):
    store, clock = Store(), Clock()
    app = create_app(
        store=store, rules=rules, credentials=service_credentials(clock()), clock=clock
    )
    return store, clock, TestClient(app)


AUTH = WRITE_AUTH


# 수집 상태 --------------------------------------------------------------------


def test_cell_states_unknown_stale_none_ok(rules):
    store, clock = Store(), Clock()
    view = View(store, rules)
    store.register_source("aws_security_hub", "env-aws")
    store.register_source("falco", "env-onprem")
    store.register_source("kyverno", "env-onprem")
    store.record_success("aws_security_hub", "env-aws", T0)
    store.record_success("kyverno", "env-onprem", T0)
    store.record_success("falco", "env-onprem", T0 - 20 * MIN)  # 15분 넘음
    s = {e["environment_id"]: e["cells"] for e in view.summary(clock())["environments"]}
    assert s["env-aws"]["THREAT"]["state"] == "OK"
    assert s["env-onprem"]["THREAT"]["state"] == "STALE"  # Falco만 위협을 본다
    assert s["env-onprem"]["VULNERABILITY"]["state"] == "NONE"  # 온프렘엔 취약점 출처가 없다
    assert s["env-onprem"]["POLICY"]["state"] == "OK"
    store.record_failure("kyverno", "env-onprem", T0, "SOURCE_UNHEALTHY")
    s = {e["environment_id"]: e["cells"] for e in view.summary(clock())["environments"]}
    assert s["env-onprem"]["POLICY"]["state"] == "UNKNOWN"
    assert s["env-onprem"]["POLICY"]["last_success_time"] == T0


def test_failure_is_never_shown_as_zero(rules):
    """수집이 실패한 칸은 건수가 0이어도 OK가 아니다."""
    store = Store()
    store.register_source("aws_security_hub", "env-aws")
    store.record_failure("aws_security_hub", "env-aws", T0, "ACCESS_DENIED")
    cells = View(store, rules).summary(T0)["environments"][0]["cells"]
    assert all(c["state"] == "UNKNOWN" for c in cells.values())


def test_source_status_documents_match_schema(rules):
    from jsonschema import Draft202012Validator

    v = Draft202012Validator({"$ref": "#/$defs/SourceStatus", "$defs": rules.schema["$defs"]})
    store = Store()
    for source, env in [
        ("falco", "e1"),
        ("kyverno", "e1"),
        ("aws_security_hub", "e2"),
        ("gcp_scc", "e3"),
    ]:
        store.register_source(source, env)
    store.record_success("falco", "e1", T0)
    store.record_failure("kyverno", "e1", T0, "SOURCE_UNHEALTHY")
    store.register_source("gcp_scc", "e3", enabled=False)
    docs = View(store, rules).statuses(T0)
    assert {d["state"] for d in docs} == {"OK", "FAILED", "STALE", "DISABLED"}
    for d in docs:
        assert not list(v.iter_errors(d)), d


def test_capabilities_show_missing_detection(rules):
    store = Store()
    store.register_source("aws_security_hub", "env-aws")
    store.register_source("falco", "env-onprem")
    caps = {
        e["environment_id"]: e["capabilities"]
        for e in View(store, rules).capabilities(T0)["environments"]
    }
    assert caps["env-aws"]["network_threat"]["available"] is True
    assert caps["env-onprem"]["network_threat"]["available"] is False  # "없음"을 숨기지 않는다
    assert caps["env-onprem"]["runtime_threat"]["sources"] == ["falco"]


@pytest.mark.parametrize("had_success", [False, True])
def test_provider_disabled_detection_is_absent_without_changing_failure_contract(rules, had_success):
    store = Store()
    store.register_source("aws_guardduty", "env-aws")
    if had_success:
        store.record_success("aws_guardduty", "env-aws", T0 - 1)
    store.record_failure("aws_guardduty", "env-aws", T0, "NOT_ENABLED")
    view = View(store, rules)
    caps = view.capabilities(T0)["environments"][0]["capabilities"]
    for name in ["network_threat", "runtime_threat"]:
        assert caps[name] == {"available": False, "sources": [], "states": {}}
    [status] = view.statuses(T0)
    assert status["state"] == "FAILED"
    assert status["error_code"] == "NOT_ENABLED"
    assert view.summary(T0)["environments"][0]["cells"]["THREAT"]["state"] == "UNKNOWN"


def test_other_source_preserves_capability_and_reenabled_source_recovers(rules):
    store = Store()
    for source in ["aws_guardduty", "aws_security_hub"]:
        store.register_source(source, "env-aws")
    store.record_failure("aws_guardduty", "env-aws", T0, "NOT_ENABLED")
    store.record_success("aws_security_hub", "env-aws", T0)
    view = View(store, rules)
    cell = view.capabilities(T0)["environments"][0]["capabilities"]["network_threat"]
    assert cell == {
        "available": True, "sources": ["aws_security_hub"],
        "states": {"aws_security_hub": "OK"},
    }
    store.record_success("aws_guardduty", "env-aws", T0 + 1)
    # A late old NOT_ENABLED result must not erase a newer success.
    store.record_failure("aws_guardduty", "env-aws", T0, "NOT_ENABLED")
    cell = view.capabilities(T0 + 1)["environments"][0]["capabilities"]["network_threat"]
    assert cell["sources"] == ["aws_guardduty", "aws_security_hub"]
    assert cell["states"] == {"aws_guardduty": "OK", "aws_security_hub": "OK"}


@pytest.mark.parametrize("code", ["ACCESS_DENIED", "TIMEOUT", "SOURCE_UNHEALTHY"])
def test_collection_failure_is_not_provider_disabled(rules, code):
    store = Store()
    store.register_source("aws_guardduty", "env-aws")
    store.record_failure("aws_guardduty", "env-aws", T0, code)
    cell = View(store, rules).capabilities(T0)["environments"][0]["capabilities"]["network_threat"]
    assert cell == {
        "available": True, "sources": ["aws_guardduty"],
        "states": {"aws_guardduty": "FAILED"},
    }


def test_coverage_api_keeps_environment_scope_when_provider_disabled(env):
    store, _, client = env
    store.register_source("aws_guardduty", "env-onprem")
    store.record_failure("aws_guardduty", "env-onprem", T0, "NOT_ENABLED")
    store.register_source("aws_guardduty", "not-authorized")
    store.record_success("aws_guardduty", "not-authorized", T0)
    response = client.get("/v1/security/coverage", headers=READ_AUTH)
    assert response.status_code == 200
    [row] = response.json()["environments"]
    assert row["environment_id"] == "env-onprem"
    assert row["capabilities"]["network_threat"]["available"] is False


# 저장 수명주기 -------------------------------------------------------------------


def test_upsert_lifecycle_create_update_close(rules):
    store, n = Store(), Normalizer(rules)
    r = raw("controls-iam-01-1-fail")
    assert (
        store.upsert(
            n.normalize("bravo_controls", r, environment_id="env-onprem", collected_time=T0)
        )
        == "created"
    )
    assert (
        store.upsert(
            n.normalize("bravo_controls", r, environment_id="env-onprem", collected_time=T0 + 1)
        )
        == "duplicate"
    )
    r["result"]["evidence"]["collected_at"] = "2026-11-04T09:02:00Z"
    r["result"].update(verdict="PASS", observed="deny")
    assert (
        store.upsert(
            n.normalize("bravo_controls", r, environment_id="env-onprem", collected_time=T0 + 2)
        )
        == "closed"
    )
    assert store.findings(environment_id="env-onprem") == []  # 해결된 것은 미해결 목록에서 빠진다
    [closed] = store.findings(environment_id="env-onprem", open_only=False)
    assert (closed["activity_id"], closed["type_uid"]) == (3, 200303)


def test_same_test_in_two_environments_does_not_collide(rules):
    store, n = Store(), Normalizer(rules)
    r = raw("controls-iam-01-1-fail")
    src = {**r, "environment": {"id": "env-aws", "role": "source", "ref": "aws:eks/source"}}
    store.upsert(n.normalize("bravo_controls", r, environment_id="env-onprem", collected_time=T0))
    store.upsert(n.normalize("bravo_controls", src, environment_id="env-aws", collected_time=T0))
    assert len(store.findings(open_only=False)) == 2


def test_silence_does_not_resolve_falco(env):
    store, clock, client = env
    client.post(
        "/v1/security/webhooks/falco/env-onprem", json=raw("onprem-falco-shell"), headers=AUTH
    )
    assert client.post("/v1/security/maintenance/resolve-silent").status_code == 401
    assert (
        client.post("/v1/security/maintenance/resolve-silent", headers=READ_AUTH).status_code == 410
    )
    clock.now = T0 + 25 * 3600 * 1000
    with pytest.raises(ValueError, match="SILENCE_IS_NOT_RESOLUTION"):
        store.resolve_silent("falco", clock.now)
    assert len(store.findings(environment_id="env-onprem")) == 1


# 밀어 넣기 API --------------------------------------------------------------------


def test_push_requires_token_and_unauthorized_does_not_change_state(env):
    store, _, client = env
    body = {"environment_id": "env-onprem", "events": [raw("onprem-kyverno-fail")]}
    assert client.post("/v1/security/ingest/kyverno", json=body).status_code == 401
    assert (
        client.post(
            "/v1/security/ingest/kyverno", json=body, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert store.sources() == []  # 미인증 요청은 상태를 만들거나 바꾸지 않는다
    other_env = {"environment_id": "env-aws", "events": []}
    assert (
        client.post("/v1/security/ingest/kyverno", json=other_env, headers=AUTH).status_code == 403
    )


def test_push_ingests_and_heartbeats(env):
    store, clock, client = env
    body = {
        "environment_id": "env-onprem",
        "events": [raw("onprem-kyverno-fail"), {"policy": "p", "result": "skip"}],
    }
    r = client.post("/v1/security/ingest/kyverno", json=body, headers=AUTH).json()
    assert r == {"recorded": "success", "created": 1, "dropped": 1}
    clock.now += 10 * MIN
    client.post(
        "/v1/security/ingest/kyverno",
        json={"environment_id": "env-onprem", "events": []},
        headers=AUTH,
    )
    [status] = client.get("/v1/security/sources", headers=READ_AUTH).json()
    assert (status["state"], status["last_success_time"]) == ("OK", clock.now)
    summary = client.get("/v1/security/summary", headers=READ_AUTH).json()["environments"][0][
        "cells"
    ]
    assert summary["POLICY"] == {"state": "OK", "counts": {"3": 1}, "sources": ["kyverno"]}


def test_agent_error_and_bad_events(env):
    store, _, client = env
    r = client.post(
        "/v1/security/ingest/falco",
        json={"environment_id": "env-onprem", "agent_error": "SOURCE_UNHEALTHY"},
        headers=AUTH,
    )
    assert r.json() == {"recorded": "failure", "error_code": "SOURCE_UNHEALTHY"}
    assert client.get("/v1/security/sources", headers=READ_AUTH).json()[0]["state"] == "FAILED"
    r = client.post(
        "/v1/security/ingest/kyverno",
        json={"environment_id": "env-onprem", "events": [{"result": "error", "policy": "p"}]},
        headers=AUTH,
    )
    assert r.json()["error_code"] == "SOURCE_UNHEALTHY"
    r = client.post(
        "/v1/security/ingest/kyverno",
        json={"environment_id": "env-onprem", "events": [{"result": "fail"}]},
        headers=AUTH,
    )
    assert r.json() == {
        "recorded": "failure",
        "quarantined": 1,
        "error_code": "INGESTION_GAP",
    }  # 필수 값 없는 한 건은 격리


def test_unknown_push_source_and_query_validation(env):
    _, _, client = env
    assert (
        client.post(
            "/v1/security/ingest/aws_security_hub",
            json={"environment_id": "env-onprem"},
            headers=AUTH,
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/security/findings", headers=READ_AUTH, params={"category": "NETWORK"}
        ).status_code
        == 422
    )
    assert (
        client.get("/v1/security/findings", headers=READ_AUTH, params={"limit": 0}).status_code
        == 422
    )


def test_t5_control_results_flow_into_the_same_screen(env):
    store, _, client = env
    body = {"environment_id": "env-onprem", "events": [raw("controls-iam-01-1-fail")]}
    client.post("/v1/security/ingest/bravo_controls", json=body, headers=AUTH)
    [f] = client.get("/v1/security/findings", headers=READ_AUTH, params={"category": "IAM"}).json()
    assert f["compliance"] == {"control": "IAM-01.1", "status": "Fail"}


# 끌어오기 수집기 --------------------------------------------------------------------


def hub_client():
    return boto3.client(
        "securityhub",
        region_name="ap-northeast-1",
        aws_access_key_id="x",
        aws_secret_access_key="x",
    )


def test_security_hub_pages_and_advances_cursor_only_on_success(rules):
    store, clock = Store(), Clock()
    ingestor = Ingestor(store, Normalizer(rules))
    client = hub_client()
    findings = [raw("aws-guardduty-via-security-hub"), raw("aws-security-hub-control")]
    for f in findings:
        f.pop("_note")
        f["AwsAccountId"] = "123456789012"
    with Stubber(client) as stub:
        stub.add_response(
            "describe_hub",
            {
                "HubArn": "arn:aws:securityhub:ap-northeast-1:123456789012:hub/default",
                "SubscribedAt": "2026-11-01T00:00:00Z",
            },
        )
        stub.add_response("get_findings", {"Findings": [findings[0]], "NextToken": "p2"})
        stub.add_response("get_findings", {"Findings": [findings[1]]})
        stub.add_response(
            "describe_hub",
            {
                "HubArn": "arn:aws:securityhub:ap-northeast-1:123456789012:hub/default",
                "SubscribedAt": "2026-11-01T00:00:00Z",
            },
        )
        counts = run_once(
            ingestor,
            SecurityHubSource(client, account_id="123456789012", region="ap-northeast-1"),
            "env-aws",
            clock,
        )
        stub.assert_no_pending_responses()
    assert counts["created"] == 2
    assert store.source("aws_security_hub", "env-aws").cursor == str(T0)

    clock.now = T0 + 5 * MIN
    findings[0]["UpdatedAt"] = "2026-11-04T10:02:00Z"
    with Stubber(client) as stub:
        stub.add_response(
            "describe_hub",
            {
                "HubArn": "arn:aws:securityhub:ap-northeast-1:123456789012:hub/default",
                "SubscribedAt": "2026-11-01T00:00:00Z",
            },
        )
        stub.add_response("get_findings", {"Findings": [findings[0]], "NextToken": "p2"})
        stub.add_client_error(
            "get_findings", service_error_code="AccessDeniedException", http_status_code=403
        )
        assert run_once(
            ingestor,
            SecurityHubSource(client, account_id="123456789012", region="ap-northeast-1"),
            "env-aws",
            clock,
        ) == {"failed": 1}
    row = store.source("aws_security_hub", "env-aws")
    assert (row.cursor, row.error_code, row.last_success_time) == (str(T0), "ACCESS_DENIED", T0)
    assert View(store, rules).statuses(clock())[0]["state"] == "FAILED"


def test_security_hub_request_uses_updated_at_window(rules):
    client = hub_client()
    with Stubber(client) as stub:
        stub.add_response(
            "describe_hub",
            {
                "HubArn": "arn:aws:securityhub:ap-northeast-1:123456789012:hub/default",
                "SubscribedAt": "2026-11-01T00:00:00Z",
            },
        )
        stub.add_response(
            "get_findings",
            {"Findings": []},
            {
                "Filters": {
                    "AwsAccountId": [{"Comparison": "EQUALS", "Value": "123456789012"}],
                    "Region": [{"Comparison": "EQUALS", "Value": "ap-northeast-1"}],
                    "UpdatedAt": [
                        {"Start": "2026-11-04T09:00:00.000Z", "End": "2026-11-04T10:00:00.000Z"}
                    ],
                },
                "MaxResults": 100,
            },
        )
        stub.add_response(
            "describe_hub",
            {
                "HubArn": "arn:aws:securityhub:ap-northeast-1:123456789012:hub/default",
                "SubscribedAt": "2026-11-01T00:00:00Z",
            },
        )
        assert (
            list(
                SecurityHubSource(client, account_id="123456789012", region="ap-northeast-1").fetch(
                    T0 - 60 * MIN, T0
                )
            )
            == []
        )


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status} AccessDenied")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, url, params, timeout):
        if "/securityCenterServices/" in url:  # detection reads ENABLED; test_gcp_scc covers the rest
            return FakeResponse({"effectiveEnablementState": "ENABLED"})
        self.calls.append((url, dict(params)))
        return self.pages.pop(0)


def test_scc_pages_and_failure(rules):
    store, clock = Store(), Clock()
    ingestor = Ingestor(store, Normalizer(rules))
    finding = raw("gcp-scc-public-bucket")
    finding.pop("_note")
    finding["name"] = "projects/123456789/sources/111/locations/global/findings/publicbucket001"
    session = FakeSession(
        [
            FakeResponse({"listFindingsResults": [{"finding": finding}], "nextPageToken": "n"}),
            FakeResponse({}),
        ]
    )
    counts = run_once(ingestor, SccSource(session, "projects/123456789"), "env-gcp", clock)
    assert counts["created"] == 1
    assert session.calls[0][0] == (
        "https://securitycenter.googleapis.com/v2/projects/123456789/sources/-/locations/global/findings"
    )
    assert session.calls[1][1]["pageToken"] == "n"
    clock.now += MIN
    failing = FakeSession([FakeResponse({}, status=403)])
    assert run_once(ingestor, SccSource(failing, "projects/123456789"), "env-gcp", clock) == {
        "failed": 1
    }
    assert store.source("gcp_scc", "env-gcp").error_code == "ACCESS_DENIED"


# 온프렘 에이전트 --------------------------------------------------------------------


def test_policy_report_both_shapes_normalize(rules):
    old = {
        "metadata": {"namespace": "demo-chat"},
        "results": [
            {
                "policy": "require-run-as-nonroot",
                "rule": "run-as-non-root",
                "result": "fail",
                "severity": "medium",
                "resources": [{"kind": "Pod", "name": "violating-pod", "namespace": "demo-chat"}],
                "timestamp": {"seconds": 1793784600},
            }
        ],
    }
    new = {
        "metadata": {"namespace": "demo-chat"},
        "scope": {"kind": "Pod", "name": "violating-pod", "namespace": "demo-chat"},
        "results": [
            {
                "policy": "require-run-as-nonroot",
                "rule": "run-as-non-root",
                "result": "fail",
                "severity": "medium",
                "timestamp": {"seconds": 1793784600},
            }
        ],
    }
    a, b = policy_report_results([old], "x"), policy_report_results([new], "x")
    assert a == b
    n = Normalizer(rules)
    f = n.normalize("kyverno", a[0], environment_id="env-onprem", collected_time=T0)
    assert f["finding_info"]["uid"].startswith("sha256:")
    assert f["resources"] == [{"type": "Pod", "uid": "violating-pod", "namespace": "demo-chat"}]


def test_agent_reports_unhealthy_falco_and_unreadable_kyverno():
    now = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)

    def healthy(path):
        if "daemonsets" in path:
            return {"status": {"desiredNumberScheduled": 1, "numberReady": 1}}
        if "/deployments/" in path:
            return {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
        return {"items": []}

    assert collect(healthy, now) == [
        {"source": "kyverno", "events": []},
        {"source": "falco", "events": []},
    ]

    # falcosidekick carries every alert, and reports-controller writes every report: either down is not "0 findings".
    def stopped(name):
        def get(path):
            if path.endswith("/deployments/" + name):
                return {"spec": {"replicas": 1}, "status": {}}
            return healthy(path)

        return {b["source"]: b.get("agent_error") for b in collect(get, now)}

    assert stopped("falco-falcosidekick") == {"kyverno": None, "falco": "SOURCE_UNHEALTHY"}
    assert stopped("kyverno-reports-controller") == {"kyverno": "SOURCE_UNHEALTHY", "falco": None}

    def broken(path):
        if "daemonsets" in path:
            return {"status": {"desiredNumberScheduled": 1, "numberReady": 0}}
        raise PermissionError("403")

    errors = {b["source"]: b.get("agent_error") for b in collect(broken, now)}
    assert errors == {"kyverno": "SOURCE_UNHEALTHY", "falco": "SOURCE_UNHEALTHY"}
