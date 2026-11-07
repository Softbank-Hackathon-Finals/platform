"""The on-prem agent entrypoint pushes what collect() reads to the real ingest API, outbound and https only."""

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

import pytest
from conftest import READ_AUTH, WRITER_TOKEN
from fastapi.testclient import TestClient

from bravo_findings import Rules
from bravo_findings.agent_run import open_no_redirect, run
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


def cluster(falco_ready=True, reports=(REPORT,)):
    def get(path):
        if "daemonsets" in path:
            return {"status": {"desiredNumberScheduled": 1, "numberReady": 1 if falco_ready else 0}}
        if "/deployments/" in path:
            return {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}
        return {"items": list(reports) if path.endswith("/policyreports") else []}

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


def test_more_results_than_one_request_takes_go_in_chunks(api, capsys):
    many = [{**REPORT, "scope": {"kind": "Pod", "name": f"demo-chat-{i}", "namespace": "target-dev"}} for i in range(1201)]
    opener = Opener(api)
    assert run(cluster(reports=many), url="https://findings.example", token=WRITER_TOKEN,
               environment_id="env-onprem", context=None, now=NOW, opener=opener) == 0
    sent = [json.loads(line)["events"] for line in capsys.readouterr().out.splitlines()]
    assert sent == [500, 500, 201, 0]  # kyverno in three requests under the API's 1000, then falco
    cells = api.get("/v1/security/summary", headers=READ_AUTH).json()["environments"][0]["cells"]
    assert cells["POLICY"]["state"] == "OK" and sum(cells["POLICY"]["counts"].values()) == 1201


def test_a_redirect_is_never_followed_with_the_credential():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(307)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/security/ingest/falco", data=b"{}",
                                     method="POST", headers={"Authorization": "Bearer x"})
    with pytest.raises(urllib.error.HTTPError) as stopped:
        open_no_redirect(request, timeout=5)
    server.server_close()
    assert stopped.value.code == 307
    assert seen == [("/v1/security/ingest/falco", "Bearer x")]


def test_a_failed_chunk_stops_the_source_and_marks_it_unknown(api, capsys):
    many = [{**REPORT, "scope": {"kind": "Pod", "name": f"demo-chat-{i}", "namespace": "target-dev"}} for i in range(1201)]

    class Flaky(Opener):
        def __call__(self, request, context=None, timeout=None):
            if len(self.seen) == 1:  # the second kyverno chunk
                self.seen.append(("POST", "lost", {}))
                raise OSError("reset")
            return super().__call__(request, context, timeout)

    opener = Flaky(api)
    assert run(cluster(reports=many), url="https://findings.example", token=WRITER_TOKEN,
               environment_id="env-onprem", context=None, now=NOW, opener=opener) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # 500 sent, 500 lost, the third chunk never sent, then the failure marker and falco
    assert [(line["source"], line["events"], line["agent_error"]) for line in lines] == [
        ("kyverno", 500, None), ("kyverno", 500, None), ("kyverno", 0, "API_ERROR"), ("falco", 0, None)]
    cells = api.get("/v1/security/summary", headers=READ_AUTH).json()["environments"][0]["cells"]
    assert cells["POLICY"]["state"] == "UNKNOWN" and cells["THREAT"]["state"] == "OK"
