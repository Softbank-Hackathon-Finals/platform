"""T42 end to end on loopback: T11 tool -> worker socket -> T38 relay -> mTLS -> real T15 server.

The worker side holds no certificate and never names run_id/attempt; the broker attaches
them from the product input it serves, and T15 routes by them to the open exec session.
"""

import hashlib
import time

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.exec_relay import ExecRelay, SocketExecChannel, t15_forwarder
from bravo_agent.product_contracts import ProductInput, validate_product_input
from bravo_agent.tools import Snapshot, Tools
from rehearsal_runner.exec_contract import inspect_record
from rehearsal_runner.exec_flow import ExecRegistry
from rehearsal_runner.exec_server import ExecServer, server_tls_context, subject_common_name
from rehearsal_runner.exec_session import ExecSession, Outcome, Space

STATUS = {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
          "ready_replicas": None, "restarts": 0, "events": None}
POD = {"target_kind": "pod", "target_name": "chat", "lines": None}


class Inspect:
    def __init__(self):
        self.calls = []

    def validate(self, arguments):
        return inspect_record(arguments)

    def run(self, arguments, deadline):
        self.calls.append(arguments)
        return Outcome(STATUS)


def _pki_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "operations/subscription/exec_pki.py"
    spec = importlib.util.spec_from_file_location("exec_pki", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    # The operator's issuance script, not test-only openssl: the round trip proves its output.
    d = tmp_path_factory.mktemp("pki") / "issued"
    summary = _pki_module().issue(d, ["rehearsal-runner"], days=1)
    assert set(summary["sha256"]) == {"broker-ca.crt", "runner-ca.crt", "broker.crt", "runner.crt"}
    assert all((d / n).stat().st_mode & 0o077 == 0 for n in ("broker.key", "runner.key"))
    return d


@pytest.fixture
def t15(pki):
    inspect = Inspect()
    registry = ExecRegistry()
    registry.register(ExecSession("run-1", 1, Space("rehearsal-a1", "uid-a"), {"inspect": inspect},
                                  secrets=lambda: [], space_uid=lambda: "uid-a"))
    tls = server_tls_context(str(pki / "runner.crt"), str(pki / "runner.key"), str(pki / "broker-ca.crt"))
    server = ExecServer(registry, ("127.0.0.1", 0), tls, subject_common_name("bravo-broker"))
    server.start()
    yield server, inspect
    server.stop()


def worker_tools(tmp_path, channel):
    (tmp_path / "src").mkdir()
    content = 'DB_HOST = "localhost"\n'
    (tmp_path / "src/config.py").write_text(content)
    manifest = SnapshotManifest(source_sha="1" * 40, files={"src/config.py": hashlib.sha256(content.encode()).hexdigest()},
                                editable_paths=["src/config.py"])
    bundle = FailureBundle(run_id="run-1", source_sha="1" * 40, failure_code="DB_CONNECTION_FAILED",
                           summary="Destination DB unavailable", evidence_digest="a" * 64)
    tools = Tools(bundle, Snapshot(tmp_path, manifest), 1, exec_channel=channel, exec_enabled=True)
    tools.set_model_budget(7, time.monotonic() + 180)
    return tools


def forwarder(server, pki):
    return t15_forwarder(f"https://127.0.0.1:{server.address[1]}", str(pki / "runner-ca.crt"),
                         str(pki / "broker.crt"), str(pki / "broker.key"), "rehearsal-runner")


def test_model_inspect_reaches_the_t15_session_through_the_broker(t15, pki, tmp_path):
    server, inspect = t15
    with ExecRelay(forwarder(server, pki), "run-1", 1) as relay:
        tools = worker_tools(tmp_path, SocketExecChannel(relay.path))
        result = tools.dispatch("mcp__bravo__rehearsal_exec_inspect", POD)
    assert result["result_code"] == "OK" and result["status"]["phase"] == "Running"
    assert inspect.calls == [POD]
    assert not relay.path.exists() and not relay.directory.exists()


def test_broker_binds_its_own_attempt_not_a_foreign_one(t15, pki, tmp_path):
    server, inspect = t15
    with ExecRelay(forwarder(server, pki), "run-1", 2) as relay:  # no session for attempt 2
        result = worker_tools(tmp_path, SocketExecChannel(relay.path)).dispatch(
            "mcp__bravo__rehearsal_exec_inspect", POD)
    assert (result["result_code"], result["reason"]) == ("REJECTED", "FOREIGN_ATTEMPT")
    assert inspect.calls == []


def test_relay_refuses_anything_but_a_local_request(t15, pki):
    server, _ = t15
    with ExecRelay(forwarder(server, pki), "run-1", 1) as relay:
        with pytest.raises(RuntimeError):
            # A worker cannot pick run_id/attempt: extra fields close the channel unanswered.
            SocketExecChannel(relay.path).send({"schema_version": 1, "request_id": "req-0001-x",
                                                "args_sha256": "0" * 64, "tool": "rehearsal.exec.inspect",
                                                "arguments": POD, "run_id": "run-1", "attempt": 1})


def test_wrong_runner_name_is_a_channel_failure(t15, pki, tmp_path):
    server, inspect = t15
    wrong = t15_forwarder(f"https://127.0.0.1:{server.address[1]}", str(pki / "runner-ca.crt"),
                          str(pki / "broker.crt"), str(pki / "broker.key"), "other-runner")
    with ExecRelay(wrong, "run-1", 1) as relay:
        result = worker_tools(tmp_path, SocketExecChannel(relay.path)).dispatch(
            "mcp__bravo__rehearsal_exec_inspect", POD)
    assert result["result_code"] in {"FAILED", "REJECTED"} and inspect.calls == []


def test_exec_flag_absent_keeps_existing_input_digest():
    from bravo_agent.contracts import digest
    fields = dict(admission_digest="a" * 64, attempt=1,
                  bundle=FailureBundle(run_id="run-1", source_sha="1" * 40, failure_code="X",
                                       summary="s", evidence_digest="b" * 64),
                  manifest=SnapshotManifest(source_sha="1" * 40,
                                            files={"a.py": hashlib.sha256(b"x\n").hexdigest()},
                                            editable_paths=["a.py"]),
                  files={"a.py": "x\n"})
    off, on = ProductInput(**fields), validate_product_input(ProductInput(**fields, exec_enabled=True))
    assert "exec_enabled" not in off.model_dump(mode="json") and on.exec_enabled is True
    assert digest(off) != digest(on) and validate_product_input(off).exec_enabled is None
