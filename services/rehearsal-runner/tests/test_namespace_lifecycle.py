import json
from types import SimpleNamespace

import pytest

from rehearsal_runner.isolation.profile import read_env
from rehearsal_runner.namespace import TEMPLATES
from rehearsal_runner.namespace_lifecycle import LifecycleUnavailable, NamespaceLifecycle
from rehearsal_runner.runner import Namespace

PROFILE = read_env(TEMPLATES.parent / "profiles" / "k3d.env")
NAME = "rehearsal-" + "a" * 24


class FakeKube:
    def __init__(self, fault=None):
        self.fault = fault
        self.calls = []
        self.gets = 0

    def run(self, *args, stdin=None):
        self.calls.append((args, stdin))
        if len(self.calls) == 1:
            if self.fault == "create_timeout":
                raise TimeoutError()
            if self.fault == "create_failed":
                return SimpleNamespace(returncode=1, stdout="")
            data = {"metadata": {"name": NAME, "uid": "uid-1"}}
            if self.fault == "wrong_name":
                data["metadata"]["name"] = "another"
            return SimpleNamespace(returncode=0, stdout=json.dumps(data))
        return SimpleNamespace(returncode=1 if self.fault == "install_failed" else 0, stdout="")

    def get_json(self, *args):
        self.gets += 1
        uid = "uid-2" if self.fault == "uid_changed" and self.gets == 2 else "uid-1"
        return {"metadata": {"uid": uid}}


class Cleaner:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def cleanup(self, name, uid):
        self.calls.append((name, uid))
        return self.result


@pytest.fixture(autouse=True)
def deterministic_name(monkeypatch):
    monkeypatch.setattr("rehearsal_runner.namespace_lifecycle.secrets.token_hex", lambda _: "a" * 24)


def test_create_uses_t3_templates_and_server_uid():
    kube, cleaner = FakeKube(), Cleaner()
    lifecycle = NamespaceLifecycle(kube, cleaner, PROFILE)
    result = lifecycle.create("run-1")
    assert result == Namespace(NAME, "uid-1")
    assert kube.calls[0][0] == ("create", "-f", "-", "-o", "json")
    assert "kind: Namespace" in kube.calls[0][1]
    assert kube.calls[1][0] == ("apply", "--server-side", "--field-manager=rehearsal-runner", "-f", "-")
    assert "kind: NetworkPolicy" in kube.calls[1][1]
    assert "kind: Namespace" not in kube.calls[1][1]
    assert kube.gets == 2
    assert lifecycle.cleanup(result) is True
    assert cleaner.calls == [(NAME, "uid-1")]


@pytest.mark.parametrize("fault", ["create_timeout", "create_failed", "wrong_name"])
def test_unknown_creation_never_cleans_an_unverified_identity(fault):
    kube, cleaner = FakeKube(fault), Cleaner()
    with pytest.raises(LifecycleUnavailable):
        NamespaceLifecycle(kube, cleaner, PROFILE).create("run-1")
    assert cleaner.calls == []


@pytest.mark.parametrize("fault", ["install_failed", "uid_changed"])
def test_setup_failure_delegates_cleanup_with_original_uid(fault):
    kube, cleaner = FakeKube(fault), Cleaner()
    with pytest.raises(LifecycleUnavailable):
        NamespaceLifecycle(kube, cleaner, PROFILE).create("run-1")
    assert cleaner.calls == [(NAME, "uid-1")]


def test_cleanup_refuses_namespace_outside_generated_scope():
    cleaner = Cleaner()
    with pytest.raises(LifecycleUnavailable):
        NamespaceLifecycle(FakeKube(), cleaner, PROFILE).cleanup(Namespace("production", "uid"))
    assert cleaner.calls == []


def test_cleanup_returns_false_when_deletion_not_confirmed():
    cleaner = Cleaner(False)
    assert NamespaceLifecycle(FakeKube(), cleaner, PROFILE).cleanup(Namespace(NAME, "uid-1")) is False


def test_runner_hands_created_uid_to_isolation_job_and_cleanup():
    from rehearsal_runner.isolation_gate import IsolationPass
    from rehearsal_runner.runner import ProposalRef, run_once

    kube, cleaner = FakeKube(), Cleaner()
    seen = []
    class Proposer:
        def propose(self, task):
            return ProposalRef(task.run_id, task.source_sha, "b" * 64)
    class Isolation:
        def check(self, namespace):
            seen.append(("isolation", namespace))
            return IsolationPass(namespace.name, namespace.uid, "probe")
    class Verifier:
        def verify(self, proposal, namespace):
            seen.append(("verify", namespace))
            return {"untrusted": True}
    task = SimpleNamespace(run_id="run-1", source_sha="a" * 40)
    result = run_once(task, Proposer(), NamespaceLifecycle(kube, cleaner, PROFILE), Isolation(), Verifier())
    assert result.state == "RECEIPT_UNVERIFIED"
    assert seen == [("isolation", Namespace(NAME, "uid-1")), ("verify", Namespace(NAME, "uid-1"))]
    assert cleaner.calls == [(NAME, "uid-1")]
