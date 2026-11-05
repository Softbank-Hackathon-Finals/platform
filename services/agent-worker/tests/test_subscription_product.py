import asyncio
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bravo_agent import subscription_product as product
from bravo_agent import subscription_server as server
from bravo_agent.contracts import FailureBundle, SnapshotManifest, digest
from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot


@pytest.fixture
def setup(tmp_path, context):
    bundle, snapshot, proposal, _ = context
    value = product.ProductInput(admission_digest="a" * 64, attempt=1, bundle=bundle,
                                 manifest=snapshot.manifest,
                                 files={name: snapshot.read(name) for name in snapshot.manifest.files})
    counts = {"auth": 0, "resolve": 0, "invoke": 0}

    def authenticate(token):
        counts["auth"] += 1
        if token != "test-identity":
            raise ValueError("secret failure detail must not escape")
        return product.ProductAccess("b" * 64, {"SessionToken": "test-private"})

    class Inputs:
        current = value

        def resolve(self, principal, input_ref):
            counts["resolve"] += 1
            assert principal == "b" * 64
            if input_ref != "input-1":
                raise ValueError("unknown input")
            return self.current

    def invoke(access, current, root, staged):
        counts["invoke"] += 1
        assert staged.read("src/config.py") == value.files["src/config.py"]

        class Provider:
            async def propose(self, bundle, tools):
                tools.dispatch("mcp__bravo__plan_propose", proposal)

        result = asyncio.run(Coordinator(current.bundle, staged).propose(Provider()))
        result["private_diagnostic"] = "test-private"
        return result

    inputs = Inputs()
    journal = product.ProposalJournal(tmp_path / "product-journal")
    service = product.ProductService(journal, authenticate, inputs, invoke, "c" * 40)
    body = {"web_identity_token": "test-identity", "input_ref": "input-1", "input_digest": digest(value)}
    return service, inputs, body, counts


def test_proposal_bound_and_retrievable_after_restart_without_model(setup):
    service, _, body, counts = setup
    status, result = service.run(body)
    assert status == 200 and result["state"] == "PROPOSAL_READY"
    assert result["input_kind"] == "product" and result["attempt"] == 1
    assert result["product_input_digest"] == body["input_digest"]
    assert result["proposal_digest"] == digest(result["proposal"])
    assert result["runtime_source_sha"] == "c" * 40
    assert result["task"] == result["input"]
    assert result["task_digest"] == result["input_digest"]
    assert result["tool_events_status"] == "UNAVAILABLE" and "tool_events" not in result
    assert "test-private" not in json.dumps(result)
    restarted = product.ProductService(product.ProposalJournal(service.journal.root), service.authenticate,
                                       service.inputs, service.invoke, service.runtime_source_sha)
    assert restarted.run(body) == (200, result)
    assert counts == {"auth": 2, "resolve": 2, "invoke": 1}
    assert not list(service.journal.root.glob("product-*"))
    data = service.journal.path.read_bytes()
    assert b"test-identity" not in data and b"test-private" not in data
    assert service.journal.path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("change", [
    {"web_identity_token": ""}, {"bundle": {}}, {"input_digest": "bad"},
    {"input_ref": "../../input"}, {"web_identity_token": "a" * 16385},
])
def test_bad_http_input_never_authenticates_or_invokes(setup, change):
    service, _, body, counts = setup
    assert service.run({**body, **change})[0] == 400
    assert counts == {"auth": 0, "resolve": 0, "invoke": 0}


def test_bad_identity_cannot_resolve_even_existing_result(setup):
    service, _, body, counts = setup
    assert service.run(body)[0] == 200
    assert service.run({**body, "web_identity_token": "forged"}) == (
        403, product.stopped("PRODUCT_ACCESS_DENIED"))
    assert counts == {"auth": 2, "resolve": 1, "invoke": 1}


def test_resolver_unavailable_does_not_claim_or_call(setup):
    service, _, body, counts = setup
    assert service.run({**body, "input_ref": "unknown"})[0] == 409
    assert counts["invoke"] == 0
    assert service.run(body)[1]["state"] == "PROPOSAL_READY"
    assert counts["invoke"] == 1


@pytest.mark.parametrize("kind", ["digest", "source", "hash", "secret", "extra_file", "retry2", "retry3"])
def test_bad_resolved_source_rejected_before_model(setup, kind):
    service, inputs, body, counts = setup
    raw = inputs.current.model_dump()
    if kind == "source":
        raw["manifest"]["source_sha"] = "d" * 40
    elif kind == "hash":
        raw["files"]["src/config.py"] = "changed source\n"
    elif kind == "secret":
        raw["files"]["src/config.py"] = 'DB_PASSWORD = "private-value-1234"\n'
        raw["manifest"]["files"]["src/config.py"] = hashlib.sha256(raw["files"]["src/config.py"].encode()).hexdigest()
    elif kind == "extra_file":
        raw["files"]["../escape"] = "bad"
    elif kind.startswith("retry"):
        raw["attempt"] = int(kind[-1])
    inputs.current = product.ProductInput.model_validate(raw)
    body["input_digest"] = "0" * 64 if kind == "digest" else digest(inputs.current)
    assert service.run(body)[0] == 409
    assert counts["invoke"] == 0
    assert not list(service.journal.root.glob("product-*"))


def test_changed_same_run_is_not_a_new_call(setup):
    service, inputs, body, counts = setup
    assert service.run(body)[1]["state"] == "PROPOSAL_READY"
    inputs.current = inputs.current.model_copy(update={"admission_digest": "d" * 64})
    body["input_digest"] = digest(inputs.current)
    assert service.run(body)[0] == 409
    assert counts["invoke"] == 1


def test_claim_survives_process_interruption_and_restart(setup):
    service, _, body, counts = setup

    def crash(*args):
        counts["invoke"] += 1
        raise KeyboardInterrupt()

    service.invoke = crash
    with pytest.raises(KeyboardInterrupt):
        service.run(body)
    restarted = product.ProductService(product.ProposalJournal(service.journal.root), service.authenticate,
                                       service.inputs, crash, service.runtime_source_sha)
    assert restarted.run(body) == (200, product.stopped("PRODUCT_OUTCOME_UNKNOWN"))
    assert counts["invoke"] == 1


def test_concurrent_call_cannot_take_claim(setup):
    service, _, body, counts = setup
    entered, release = threading.Event(), threading.Event()
    original = service.invoke

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    service.invoke = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.run, body)
        assert entered.wait(5)
        try:
            assert service.run(body) == (200, product.stopped("PRODUCT_OUTCOME_UNKNOWN"))
        finally:
            release.set()
        assert first.result(timeout=5)[1]["state"] == "PROPOSAL_READY"
    assert counts["invoke"] == 1


@pytest.mark.parametrize("fault", ["exception", "other_source", "bad_edit", "fixture",
                                  "task_missing", "task_changed", "task_digest"])
def test_unconfirmed_worker_is_terminal_without_reexecution(setup, fault):
    service, _, body, counts = setup
    original = service.invoke

    def invalid(*args):
        result = original(*args)
        if fault == "exception":
            raise ValueError("access_token=never-publish-this-value")
        if fault == "other_source":
            result["source_sha"] = "0" * 40
        elif fault == "bad_edit":
            result["proposal"]["edits"][0]["path"] = "src/unapproved.py"
            result["proposal_digest"] = digest(result["proposal"])
        elif fault == "fixture":
            result["input_kind"] = "synthetic_db_host_fixture"
        elif fault == "task_missing":
            result.pop("task")
        elif fault == "task_changed":
            result["task"]["summary"] = "Different original objective"
            result["task_digest"] = digest(result["task"])
        else:
            result["task_digest"] = "0" * 64
        return result

    service.invoke = invalid
    expected = (200, product.stopped("PRODUCT_RUNTIME_UNCONFIRMED"))
    assert service.run(body) == expected
    assert service.run(body) == expected
    assert counts["invoke"] == 1
    assert b"never-publish" not in service.journal.path.read_bytes()


def test_damaged_saved_result_does_not_replay(setup):
    service, _, body, counts = setup
    assert service.run(body)[0] == 200
    with sqlite3.connect(service.journal.path) as db:
        db.execute("UPDATE proposals SET result='{}'")
    assert service.run(body)[0] == 409
    assert counts["invoke"] == 1


def test_journal_rejects_symlink(tmp_path):
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    other = tmp_path / "elsewhere"
    other.write_text("unchanged")
    (root / "proposals.sqlite3").symlink_to(other)
    with pytest.raises(ValueError):
        product.ProposalJournal(root)
    assert other.read_text() == "unchanged"


def test_host_bridge_uses_sts_identity_and_existing_worker(monkeypatch, setup, context):
    service, inputs, body, _ = setup
    _, _, proposal, _ = context
    calls = []
    credentials = {"AccessKeyId": "test-access", "SecretAccessKey": "test-private", "SessionToken": "test-session"}

    class STS:
        def assume_role_with_web_identity(self, **kwargs):
            calls.append(kwargs)
            return {"Credentials": credentials, "Provider": "test-issuer", "Audience": "test-audience",
                    "SubjectFromWebIdentityToken": "test-subject"}

    def client(name, **kwargs):
        calls.append({"service": name, **kwargs})
        return STS() if name == "sts" else object()

    def worker(store, state, args):
        bundle = FailureBundle.model_validate_json(args.bundle.read_text())
        manifest = SnapshotManifest.model_validate_json(args.manifest.read_text())

        class Provider:
            async def propose(self, bundle, tools):
                tools.dispatch("mcp__bravo__plan_propose", proposal)

        result = asyncio.run(Coordinator(bundle, Snapshot(args.snapshot, manifest)).propose(Provider()))
        args.output.write_text(json.dumps(result))
        return 0

    class Inputs:
        def resolve(self, principal, input_ref):
            assert principal == digest({"Provider": "test-issuer", "Audience": "test-audience",
                                        "SubjectFromWebIdentityToken": "test-subject"})
            return inputs.current

    monkeypatch.setattr(server.boto3, "client", client)
    monkeypatch.setattr(product, "SubscriptionStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(product, "run_worker", worker)
    settings = {"region": "test-region", "broker_role_arn": "test-role", "secret_arn": "test-secret",
                "runtime_host_id": "test-host", "state_dir": str(service.journal.root / "state"),
                "model": "test-model", "source_sha": "c" * 40}
    bridge = server.product_service(settings, Inputs(), service.journal.root)
    assert bridge.run(body)[1]["state"] == "PROPOSAL_READY"
    assert calls[1]["WebIdentityToken"] == body["web_identity_token"]
    assert calls[2]["service"] == "secretsmanager"
    assert calls[2]["aws_session_token"] == credentials["SessionToken"]
    assert not list(service.journal.root.glob("product-*"))


def test_configured_http_route_keeps_product_and_fixture_separate(monkeypatch, setup):
    import http.client
    import queue
    from http.server import HTTPServer

    service, _, body, counts = setup
    ready = queue.Queue()

    def bind(address, handler):
        listener = HTTPServer((address[0], 0), handler)
        ready.put(listener.server_port)
        return listener

    monkeypatch.setattr(server, "HTTPServer", bind)
    monkeypatch.setattr(server, "execute_fixture", lambda *args: pytest.fail("product cannot execute fixture"))
    thread = threading.Thread(target=server.serve, args=({},), kwargs={"product": service}, daemon=True)
    thread.start()
    port = ready.get(timeout=5)

    def post(payload):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST", "/run", json.dumps(payload))
            response = connection.getresponse()
            assert response.getheader("Cache-Control") == "no-store"
            return response.status, json.load(response)
        finally:
            connection.close()

    assert post({**body, "bundle": {}})[0] == 400
    assert post(body)[1]["input_kind"] == "product"
    thread.join(5)
    assert not thread.is_alive()
    assert counts["invoke"] == 1
