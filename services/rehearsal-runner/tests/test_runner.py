from types import SimpleNamespace

import pytest

from rehearsal_runner.isolation_gate import IsolationBlocked, IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once


class Ports:
    def __init__(self, fault=None):
        self.events = []
        self.fault = fault
        self.receipt = {"sensitive": "not-for-logs"}
        self.ns = Namespace("rehearsal-test", "uid-1")

    def propose(self, task):
        self.events.append("propose")
        if self.fault == "proposal_error":
            raise RuntimeError("private provider details")
        return ProposalRef("wrong" if self.fault == "proposal_mismatch" else task.run_id,
                           "a" * 40, "b" * 64)

    def create(self, run_id):
        self.events.append("create")
        if self.fault == "create_error":
            raise RuntimeError("unknown remote effect")
        if self.fault == "identity_missing":
            return Namespace("rehearsal-test", "")
        if self.fault == "outside_namespace":
            return Namespace("production", "uid-other")
        return self.ns

    def check(self, namespace):
        self.events.append("isolation")
        assert namespace == self.ns
        if self.fault == "blocked":
            raise IsolationBlocked("failed")
        if self.fault == "isolation_error":
            raise RuntimeError("unavailable")
        return IsolationPass(namespace.name, "other" if self.fault == "uid_changed" else namespace.uid, "probe-1")

    def verify(self, proposal, namespace):
        self.events.append("verify")
        assert namespace == self.ns
        if self.fault == "verifier_error":
            raise RuntimeError("unknown remote effect")
        return None if self.fault == "missing_receipt" else self.receipt

    def cleanup(self, namespace):
        self.events.append("cleanup")
        assert namespace == self.ns
        if self.fault == "cleanup_error":
            raise RuntimeError("cleanup failed")
        return False if self.fault == "cleanup_unknown" else True


def execute(ports, task=None):
    if task is None:
        task = SimpleNamespace(run_id="run-1", source_sha="a" * 40)
    return run_once(task, ports, ports, ports, ports)


def test_success_preserves_order_and_returns_only_unverified_receipt():
    ports = Ports()
    result = execute(ports)
    assert ports.events == ["propose", "create", "isolation", "verify", "cleanup"]
    assert result.state == "RECEIPT_UNVERIFIED"
    assert result.untrusted_receipt is ports.receipt
    assert "not-for-logs" not in repr(result)


@pytest.mark.parametrize("fault,reason,events", [
    ("proposal_error", "PROPOSAL_UNAVAILABLE", ["propose"]),
    ("proposal_mismatch", "PROPOSAL_INVALID", ["propose"]),
    ("create_error", "NAMESPACE_CREATE_UNKNOWN", ["propose", "create"]),
    ("identity_missing", "NAMESPACE_IDENTITY_UNKNOWN", ["propose", "create"]),
    ("outside_namespace", "NAMESPACE_IDENTITY_UNKNOWN", ["propose", "create"]),
    ("blocked", "ISOLATION_BLOCKED", ["propose", "create", "isolation", "cleanup"]),
    ("uid_changed", "ISOLATION_IDENTITY_MISMATCH", ["propose", "create", "isolation", "cleanup"]),
    ("isolation_error", "EFFECTS_UNKNOWN", ["propose", "create", "isolation", "cleanup"]),
    ("verifier_error", "EFFECTS_UNKNOWN", ["propose", "create", "isolation", "verify", "cleanup"]),
    ("missing_receipt", "RECEIPT_MISSING", ["propose", "create", "isolation", "verify", "cleanup"]),
    ("cleanup_unknown", "CLEANUP_UNCONFIRMED", ["propose", "create", "isolation", "verify", "cleanup"]),
    ("cleanup_error", "CLEANUP_UNCONFIRMED", ["propose", "create", "isolation", "verify", "cleanup"]),
])
def test_failure_stops_or_holds_and_never_releases_receipt(fault, reason, events):
    ports = Ports(fault)
    result = execute(ports)
    assert result.reason == reason
    assert result.state in {"BLOCK", "NEEDS_HUMAN"}
    assert result.untrusted_receipt is None
    assert ports.events == events


def test_wrong_revision_stops_before_namespace_creation():
    ports = Ports()
    result = execute(ports, SimpleNamespace(run_id="run-1", source_sha="c" * 40))
    assert result.reason == "PROPOSAL_INVALID"
    assert ports.events == ["propose"]


def test_invalid_task_never_calls_any_port():
    ports = Ports()
    assert execute(ports, SimpleNamespace(run_id="../invalid")).reason == "INVALID_TASK"
    assert ports.events == []


def test_t3_adapter_calls_existing_gate_with_namespace(monkeypatch):
    from rehearsal_runner import runner

    calls = []
    expected = IsolationPass("rehearsal-test", "uid-1", "probe-1")
    def check(kube, name, targets, image):
        calls.append((kube, name, targets, image))
        return expected
    monkeypatch.setattr(runner, "check_isolation", check)
    gate = runner.T3Gate(kube="mock-kube", targets=[], image="test-image")
    assert gate.check(Namespace("rehearsal-test", "uid-1")) == expected
    assert calls == [("mock-kube", "rehearsal-test", [], "test-image")]
