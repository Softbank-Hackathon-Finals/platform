"""Exercise T15's isolation-to-handoff path with a simulated job port."""

from types import SimpleNamespace

from rehearsal_runner.integration_handoff import HandoffVerifier
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once


class Ports:
    def __init__(self, *, wrong_uid=False, cleanup=True):
        self.events = []
        self.requests = []
        self.wrong_uid = wrong_uid
        self.cleanup_result = cleanup

    def propose(self, task):
        self.events.append("propose")
        return ProposalRef(task.run_id, task.source_sha, "b" * 64)

    def create(self, run_id):
        self.events.append("create")
        return Namespace("rehearsal-test", "uid-1")

    def check(self, namespace):
        self.events.append("isolation")
        return IsolationPass(namespace.name, "wrong" if self.wrong_uid else namespace.uid, "probe-1")

    def verify(self, request):
        self.events.append("job")
        self.requests.append(request)
        return {"result": "untrusted"}

    def cleanup(self, namespace):
        self.events.append("cleanup")
        return self.cleanup_result


def run(ports, **kwargs):
    task = SimpleNamespace(run_id="run-1", source_sha="a" * 40)
    adapter = HandoffVerifier(
        ports, attempt=1, diff=b"candidate diff", image_digest="sha256:" + "c" * 64,
        target_environment_id="onprem",
    )
    return run_once(task, ports, ports, ports, None, isolated_verifier=adapter, **kwargs)


def test_real_runner_delivers_original_isolation_identity_to_simulated_job():
    ports = Ports()
    result = run(ports)
    assert ports.events == ["propose", "create", "isolation", "job", "cleanup"]
    assert ports.requests[0].namespace == Namespace("rehearsal-test", "uid-1")
    assert ports.requests[0].isolation_probe_run_id == "probe-1"
    assert result.state == "RECEIPT_UNVERIFIED"


def test_changed_uid_prevents_job_submission_and_still_cleans_up():
    ports = Ports(wrong_uid=True)
    result = run(ports)
    assert result.reason == "ISOLATION_IDENTITY_MISMATCH"
    assert ports.events == ["propose", "create", "isolation", "cleanup"]
    assert ports.requests == []


def test_unconfirmed_cleanup_never_returns_job_receipt():
    ports = Ports(cleanup=False)
    result = run(ports)
    assert result.reason == "CLEANUP_UNCONFIRMED"
    assert result.untrusted_receipt is None


def test_ambiguous_verifier_configuration_stops_before_namespace_creation():
    ports = Ports()
    task = SimpleNamespace(run_id="run-1", source_sha="a" * 40)
    adapter = HandoffVerifier(ports, 1, b"diff", "sha256:" + "c" * 64, "onprem")
    result = run_once(task, ports, ports, ports, ports, isolated_verifier=adapter)
    assert result.reason == "VERIFIER_CONFIGURATION_INVALID"
    assert ports.events == ["propose"]
