import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from exec_schema import assert_valid

from rehearsal_runner.exec_actions import (
    PROBE_SCRIPT,
    Inspector,
    Prober,
    Result,
    tool_pod_manifest,
)
from rehearsal_runner.exec_session import ActionDenied, ActionFailed, NotSent, Space, Transient

SPACE = Space("rehearsal-plan-1-a", "uid-a")
IDENTITY = ["--as", "system:serviceaccount:rehearsal-plan-1-a:rehearsal-exec", "-n", "rehearsal-plan-1-a"]
IMAGE = "docker.io/library/python@sha256:" + "e" * 64


class Kubectl:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, args, timeout):
        self.calls.append((args, timeout))
        return self.results.pop(0)


def ok(payload) -> Result:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return Result(0, data, "")


def far():
    return 10**9


def test_pod_status_is_read_as_rehearsal_exec_in_space_a():
    pod = {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                      "containerStatuses": [{"restartCount": 2}, {"restartCount": 1}]}}
    kube = Kubectl(ok(pod))
    out = Inspector(SPACE, kube).run({"target_kind": "pod", "target_name": "chat-1", "lines": None}, far())
    assert kube.calls[0][0] == IDENTITY + ["get", "pod", "chat-1", "-o", "json"]
    assert out.status | {} == {"kind": "inspect", "found": True, "phase": "Running", "ready": True,
                               "replicas": None, "ready_replicas": None, "restarts": 3, "events": None}
    assert_valid("inspect_status", out.status)


def test_deployment_missing_and_found():
    kube = Kubectl(Result(1, b"", 'Error from server (NotFound): deployments.apps "chat" not found'),
                   ok({"spec": {"replicas": 2}, "status": {"readyReplicas": 1}}))
    inspector = Inspector(SPACE, kube)
    args = {"target_kind": "deployment", "target_name": "chat", "lines": None}
    assert inspector.run(args, far()).status["found"] is False
    status = inspector.run(args, far()).status
    assert (status["replicas"], status["ready_replicas"], status["ready"]) == (2, 1, False)
    assert_valid("inspect_status", status)


def test_events_and_logs_return_screenable_output():
    events = {"items": [{"type": "Warning", "reason": "BackOff", "message": "restarting",
                         "involvedObject": {"kind": "Pod", "name": "chat-1"}, "lastTimestamp": "2"},
                        {"type": "Normal", "reason": "Pulled", "message": "image pulled",
                         "involvedObject": {"kind": "Pod", "name": "chat-1"}, "lastTimestamp": "1"}]}
    kube = Kubectl(ok(events), ok(b"started\n"))
    inspector = Inspector(SPACE, kube, log_container="app")
    out = inspector.run({"target_kind": "events", "target_name": None, "lines": None}, far())
    assert out.status["events"] == 2 and out.output.decode().splitlines()[0].startswith("Normal Pulled")
    out = inspector.run({"target_kind": "logs", "target_name": "chat-1", "lines": 20}, far())
    assert kube.calls[1][0] == IDENTITY + ["logs", "pod/chat-1", "--tail=20", "-c", "app"]
    assert out.output == b"started\n"


@pytest.mark.parametrize("stderr,error", [
    ('Error from server (Forbidden): pods is forbidden', ActionDenied),
    ("Unable to connect to the server: dial tcp: connection refused", NotSent),
    ("error: something odd", Transient),
])
def test_api_errors_are_classified(stderr, error):
    with pytest.raises(error):
        Inspector(SPACE, Kubectl(Result(1, b"", stderr))).run(
            {"target_kind": "pod", "target_name": "chat", "lines": None}, far())


def test_no_call_after_the_deadline():
    kube = Kubectl()
    with pytest.raises(NotSent):
        Inspector(SPACE, kube, clock=lambda: 10.0).run(
            {"target_kind": "pod", "target_name": "chat", "lines": None}, 5.0)
    assert kube.calls == []


def test_probe_runs_platform_script_with_argv_only():
    kube = Kubectl(ok(b'{"probe_outcome":"CONNECTED","http_status":403,"elapsed_ms":7}\n<Error>AccessDenied'))
    out = Prober(SPACE, kube).run({"service": "seaweedfs", "port": 8333, "method": "HTTP_GET",
                                   "path": "/bucket/key"}, far())
    args = kube.calls[0][0]
    assert args[:len(IDENTITY)] == IDENTITY
    assert args[len(IDENTITY):len(IDENTITY) + 9] == ["exec", "rehearsal-exec-tool", "-c", "tool", "--",
                                                     "python3", "-I", "-c", PROBE_SCRIPT]
    assert args[-5:-1] == ["seaweedfs.rehearsal-plan-1-a.svc", "8333", "HTTP_GET", "/bucket/key"]
    assert out.status == {"kind": "probe", "probe_outcome": "CONNECTED", "http_status": 403, "elapsed_ms": 7}
    assert out.record == {"probe_outcome": "CONNECTED", "http_status": 403}
    assert out.output == b"<Error>AccessDenied"
    assert_valid("probe_status", out.status)


@pytest.mark.parametrize("result,error", [
    (Result(1, b"", 'Error from server (NotFound): pods "rehearsal-exec-tool" not found'), NotSent),
    (Result(1, b"", "error: unable to upgrade connection: container not found"), NotSent),
    (Result(1, b"", "pods is forbidden"), ActionDenied),
    (Result(126, b"", "command terminated with exit code 126"), ActionFailed),
    (Result(0, b"not json\n", ""), ActionFailed),
    (Result(0, b'{"probe_outcome":"OPEN","http_status":null,"elapsed_ms":1}\n', ""), ActionFailed),
])
def test_probe_failures(result, error):
    with pytest.raises(error):
        Prober(SPACE, Kubectl(result)).run({"service": "db", "port": 5432, "method": "TCP", "path": None}, far())


# -- the probe script itself, executed locally -----------------------------------------------

def run_script(host, port, method, path="/", timeout="2"):
    out = subprocess.run([sys.executable, "-I", "-c", PROBE_SCRIPT, host, str(port), method, path, timeout],
                         capture_output=True, timeout=20)
    assert out.returncode == 0
    head, _, body = out.stdout.partition(b"\n")
    return json.loads(head), body


class Deny(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"<Error>AccessDenied</Error>")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def http_port():
    server = HTTPServer(("127.0.0.1", 0), Deny)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_script_outcomes(http_port):
    observed, body = run_script("127.0.0.1", http_port, "HTTP_GET", "/bucket/key")
    assert (observed["probe_outcome"], observed["http_status"], body) == ("CONNECTED", 403,
                                                                          b"<Error>AccessDenied</Error>")
    observed, body = run_script("127.0.0.1", http_port, "HTTP_HEAD")
    assert (observed["http_status"], body) == (200, b"")
    assert run_script("127.0.0.1", http_port, "TCP")[0]["probe_outcome"] == "CONNECTED"
    assert run_script("127.0.0.1", free_port(), "TCP")[0]["probe_outcome"] == "REFUSED"
    assert run_script("no-such-service.invalid", 80, "TCP")[0]["probe_outcome"] == "DNS_FAILED"


# -- tool Pod template ---------------------------------------------------------------------

def test_tool_pod_has_no_token_secret_or_model_controlled_field():
    pod = tool_pod_manifest(SPACE, IMAGE, "plan-1-a", 1)
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False and spec["serviceAccountName"] == "default"
    assert "volumes" not in spec and pod["metadata"]["namespace"] == SPACE.name
    text = json.dumps(pod)
    assert "secretKeyRef" not in text and "envFrom" not in text and "hostPath" not in text
    assert spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["metadata"]["labels"]["bravo.platform/attempt"] == "1"


def test_tool_image_must_be_pinned():
    with pytest.raises(ValueError):
        tool_pod_manifest(SPACE, "python:3.12", "plan-1-a", 1)
