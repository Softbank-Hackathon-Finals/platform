"""Mock T15 control flow; not an authorization or T38 interface."""

from types import SimpleNamespace

import pytest

from rehearsal_runner import isolation_gate
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.isolation.model import Kind, Result, Status


class FakeKube:
    def __init__(self, uids=("uid-1", "uid-1", "uid-1"), fixture_items=()):
        self.uids = iter(uids)
        self.fixture_items = list(fixture_items)

    def get_json(self, *args):
        if args[0] == "namespace":
            return {"metadata": {"uid": next(self.uids), "labels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}}}
        return {"items": self.fixture_items}

    def run(self, *args, **kwargs):
        return SimpleNamespace(returncode=0)


class FakeVerificationJob:
    def __init__(self):
        self.requests = []

    def submit(self, task, namespace, namespace_uid):
        self.requests.append((task.run_id, namespace, namespace_uid))


def simulate_flow(kube, verifier):
    # Task preparation is read-only. Only an isolation pass reaches the mock job.
    task = prepare_bucket_policy_task("run-1", '{"Statement":[]}', "rehearsal-test", source_sha="a" * 40)
    result = isolation_gate.check_isolation(kube, "rehearsal-test", [], "test-image", cleanup_timeout=0)
    verifier.submit(task, result.namespace, result.namespace_uid)


@pytest.mark.parametrize("status", [Status.FAIL, Status.INCONCLUSIVE])
def test_failed_or_unknown_isolation_never_starts_job(monkeypatch, status):
    monkeypatch.setattr(isolation_gate, "run", lambda *_: [Result("N", "test", Kind.ISOLATION, status)])
    verifier = FakeVerificationJob()
    with pytest.raises(isolation_gate.IsolationBlocked):
        simulate_flow(FakeKube(), verifier)
    assert verifier.requests == []


def test_pass_forwards_tested_namespace_identity(monkeypatch):
    monkeypatch.setattr(isolation_gate, "run", lambda *_: [Result("N", "test", Kind.ISOLATION, Status.PASS)])
    verifier = FakeVerificationJob()
    simulate_flow(FakeKube(), verifier)
    assert verifier.requests == [("run-1", "rehearsal-test", "uid-1")]


@pytest.mark.parametrize("kube", [FakeKube(uids=("uid-1", "uid-2")),
                                FakeKube(fixture_items=[{"metadata": {"name": "remaining"}}])])
def test_uid_change_or_fixture_leak_never_starts_job(monkeypatch, kube):
    monkeypatch.setattr(isolation_gate, "run", lambda *_: [Result("N", "test", Kind.ISOLATION, Status.PASS)])
    verifier = FakeVerificationJob()
    with pytest.raises(isolation_gate.IsolationBlocked):
        simulate_flow(kube, verifier)
    assert verifier.requests == []
