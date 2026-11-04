import asyncio
import http.client
import json
import queue
import threading
from http.server import HTTPServer

import pytest

from bravo_agent import subscription_server as server
from bravo_agent.contracts import FailureBundle
from bravo_agent.coordinator import Coordinator


def test_broker_uses_web_identity_in_memory_and_reports_rotation(monkeypatch, tmp_path, context):
    bundle, snapshot, proposal, _ = context
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "failure.json").write_text(bundle.model_dump_json())
    (examples / "snapshot-manifest.json").write_text(snapshot.manifest.model_dump_json())
    calls = []
    credentials = {"AccessKeyId": "test-access", "SecretAccessKey": "test-private", "SessionToken": "test-session"}

    class STS:
        def assume_role_with_web_identity(self, **kwargs):
            calls.append(kwargs)
            return {"Credentials": credentials}

    def client(name, **kwargs):
        calls.append({"service": name, **kwargs})
        return STS() if name == "sts" else object()

    class Store:
        def __init__(self, *args, **kwargs):
            self.count = 0

        def read(self):
            self.count += 1
            return {"state": "ACTIVE", "client_id": "same-client", "subject": "same-subject",
                    "access_token": str(self.count), "refresh_token": str(self.count),
                    "expires_at": self.count}, str(self.count)

    def worker(store, state, args):
        assert args.snapshot == tmp_path / "examples/source"
        actual = FailureBundle.model_validate_json(args.bundle.read_text())

        class Provider:
            async def propose(self, bundle, tools):
                tools.dispatch("mcp__bravo__plan_propose", {**proposal, "run_id": actual.run_id})

        result = asyncio.run(Coordinator(actual, snapshot).propose(Provider()))
        args.output.write_text(json.dumps(result))
        return 0

    monkeypatch.setattr(server.boto3, "client", client)
    monkeypatch.setattr(server, "SubscriptionStore", Store)
    monkeypatch.setattr(server, "run_worker", worker)
    settings = {"region": "ap-northeast-2", "broker_role_arn": "test-role",
                             "secret_arn": "test-secret", "runtime_host_id": "test-host",
                             "worker": str(tmp_path), "state_dir": str(tmp_path / "state"),
                             "model": "test-model", "source_sha": "a" * 40}
    run_id = "subscription-" + "a" * 32
    result = server.execute_fixture(settings, "test-web-identity", run_id)
    assert calls[1] == {"RoleArn": "test-role", "RoleSessionName": "bravo-subscription-vm",
                        "WebIdentityToken": "test-web-identity", "DurationSeconds": 900}
    assert calls[2]["aws_session_token"] == credentials["SessionToken"]
    assert all(result[name] for name in ("credential_rotated", "access_rotated", "refresh_rotated",
                                        "registration_reused", "subject_reused", "expires_at_increased"))
    assert "test-web-identity" not in json.dumps(result)
    assert "test-private" not in json.dumps(result)
    published = tmp_path / "runs" / run_id / "proposal.json"
    original = published.read_bytes()
    assert result["result"]["input_kind"] == json.loads(original)["input_kind"] == "synthetic_db_host_fixture"
    assert published.stat().st_mode & 0o222 == 0
    second = server.execute_fixture(settings, "test-web-identity", "subscription-" + "b" * 32)
    assert second["result"]["run_id"] != result["result"]["run_id"]
    assert published.read_bytes() == original
    with pytest.raises(FileExistsError):
        server.execute_fixture(settings, "test-web-identity", run_id)
    assert published.read_bytes() == original
    assert sum(call.get("service") == "secretsmanager" for call in calls) == 2


def test_rejected_identity_never_reads_secret_or_starts_model(monkeypatch):
    class STS:
        def assume_role_with_web_identity(self, **kwargs):
            raise ValueError("identity denied")

    monkeypatch.setattr(server.boto3, "client", lambda name, **kwargs: STS())
    monkeypatch.setattr(server, "run_worker", lambda *args: pytest.fail("must not start model"))
    with pytest.raises(ValueError, match="identity denied"):
        server.execute_fixture({"region": "ap-northeast-2", "broker_role_arn": "test-role"}, "invalid",
                       "subscription-" + "a" * 32)


def test_product_route_cannot_dispatch_fixtures_or_consume_validation_slots(monkeypatch):
    ready = queue.Queue()
    calls = []

    def bind(address, handler):
        listener = HTTPServer((address[0], 0), handler)
        ready.put(listener.server_port)
        return listener

    def fixture(settings, token, run_id):
        calls.append(run_id)
        return {"state": "PROPOSAL_READY", "result": {"input_kind": "synthetic_db_host_fixture"}}

    monkeypatch.setattr(server, "HTTPServer", bind)
    monkeypatch.setattr(server, "execute_fixture", fixture)
    thread = threading.Thread(target=server.serve, args=({},), daemon=True)
    thread.start()
    port = ready.get(timeout=5)

    def post(path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", path, json.dumps(payload))
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()

    body = {"web_identity_token": "synthetic", "run_id": "subscription-" + "a" * 32}
    for payload in (body, {**body, "input_kind": "synthetic_db_host_fixture"},
                    {**body, "bundle": {"source_sha": "1" * 40}}):
        assert post("/run", payload) == (503, {"state": "NEEDS_HUMAN", "reason": "PRODUCT_INPUT_UNAVAILABLE"})
    assert post("/validation/fixture", {**body, "snapshot": "elsewhere"})[0] == 400
    assert not calls
    assert post("/validation/fixture", body)[0] == 200
    other = {**body, "run_id": "subscription-" + "b" * 32}
    assert post("/validation/fixture", other)[0] == 200
    thread.join(5)
    assert not thread.is_alive()
    assert calls == [body["run_id"], other["run_id"]]
