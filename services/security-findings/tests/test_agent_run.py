"""The on-prem agent entrypoint pushes what collect() reads to the real ingest API, outbound and https only."""

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from conftest import READ_AUTH, WRITER_TOKEN
from fastapi.testclient import TestClient

from bravo_findings import Rules
from bravo_findings.agent_run import run
from bravo_findings.api import create_app
from bravo_findings.store import Store

NOW = datetime(2026, 11, 8, 4, 0, tzinfo=UTC)
T0 = int(NOW.timestamp() * 1000)

REPORT = {
    "metadata": {"namespace": "target-dev"},
    "scope": {"kind": "Pod", "name": "demo-chat-x", "namespace": "target-dev"},
    "results": [{"policy": "require-run-as-nonroot", "rule": "run-as-non-root", "result": "fail",
                 "severity": "medium", "message": "runAsNonRoot must be true", "timestamp": {"seconds": T0 // 1000}}],
}


def cluster(falco_ready=True):
    def get(path):
        if "daemonsets" in path:
            return {"status": {"desiredNumberScheduled": 1, "numberReady": 1 if falco_ready else 0}}
        return {"items": [REPORT] if path.endswith("/policyreports") else []}

    return get


class Opener:
    """urllib.urlopen stand-in that sends the request to the in-process API."""

    def __init__(self, client):
        self.client, self.seen = client, []

    def __call__(self, request, context=None, timeout=None):
        path = urlsplit(request.full_url).path
        self.seen.append((request.get_method(), path, dict(request.header_items())))
        response = self.client.post(path, content=request.data, headers=dict(request.header_items()))
        if response.status_code >= 400:
            raise OSError(response.status_code)
        return _Response(response.status_code)


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def api(service_credentials):
    app = create_app(store=Store(), rules=Rules(), credentials=service_credentials(T0), clock=lambda: T0)
    return TestClient(app)


def test_agent_pushes_policy_reports_and_falco_health_to_the_ingest_api(api, capsys):
    opener = Opener(api)
    code = run(cluster(), url="https://findings.example", token=WRITER_TOKEN, environment_id="env-onprem",
               context=None, now=NOW, opener=opener)
    assert code == 0
    assert [(m, p) for m, p, _ in opener.seen] == [("POST", "/v1/security/ingest/kyverno"),
                                                  ("POST", "/v1/security/ingest/falco")]
    findings = api.get("/v1/security/findings?environment_id=env-onprem&status=all", headers=READ_AUTH).json()
    assert len(findings) == 1 and findings[0]["bravo"]["category"] == "POLICY"
    cells = api.get("/v1/security/summary", headers=READ_AUTH).json()["environments"][0]["cells"]
    assert cells["POLICY"]["state"] == "OK" and cells["THREAT"]["state"] == "OK"
    out = capsys.readouterr().out
    assert WRITER_TOKEN not in out and "runAsNonRoot" not in out  # counts only, never bodies or credentials


def test_unready_falco_turns_the_threat_cell_unknown(api):
    assert run(cluster(falco_ready=False), url="https://findings.example", token=WRITER_TOKEN,
               environment_id="env-onprem", context=None, now=NOW, opener=Opener(api)) == 0
    cells = api.get("/v1/security/summary", headers=READ_AUTH).json()["environments"][0]["cells"]
    assert cells["THREAT"]["state"] == "UNKNOWN"


def test_rejected_push_fails_the_run_and_plain_http_is_refused(api, capsys):
    assert run(cluster(), url="https://findings.example", token="wrong-" + "x" * 40, environment_id="env-onprem",
               context=None, now=NOW, opener=Opener(api)) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(line["error"] == "OSError" for line in lines)
    with pytest.raises(ValueError):
        run(cluster(), url="http://findings.example", token=WRITER_TOKEN, environment_id="env-onprem",
            context=None, now=NOW, opener=Opener(api))
