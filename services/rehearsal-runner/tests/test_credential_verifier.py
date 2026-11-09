from dataclasses import replace

import pytest

from rehearsal_runner.credential_probe import CredentialObservations, observe_credential_delivery
from rehearsal_runner.credential_verifier import CredentialVerifier
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once
from rehearsal_runner.secret_task import prepare_credential_migration

REVISION = "a" * 40
NAMESPACE = Namespace("rehearsal-credentials", "uid-original")


class Probe:
    def __init__(self, *, secrets=("yes", "yes"), db="yes", access="no", error=False):
        self.secrets = iter(secrets)
        self.db = db
        self.access = access
        self.error = error

    def secret_exists(self):
        return next(self.secrets)

    def app_connects(self):
        if self.error:
            raise RuntimeError("private fixture information")
        return self.db

    def other_account_can_read(self):
        return self.access


@pytest.mark.parametrize("probe,expected", [
    (Probe(), CredentialObservations("yes", "yes", "no", "yes")),
    (Probe(secrets=("no", "no")), CredentialObservations("no", "yes", "no", "no")),
    (Probe(secrets=("yes", "no")), CredentialObservations("yes", "yes", "no", "no")),
    (Probe(db="no", access="yes"), CredentialObservations("yes", "no", "yes", "yes")),
    (Probe(access="unexpected"), CredentialObservations("yes", "yes", "unknown", "yes")),
    (Probe(error=True), CredentialObservations("unknown", "unknown", "unknown", "unknown")),
])
def test_credential_observations_keep_controls_and_ambiguity(probe, expected):
    result = observe_credential_delivery(probe)
    assert result == expected
    assert not hasattr(result, "verdict")
    assert "private fixture information" not in repr(result)


class Job:
    def __init__(self):
        self.requests = []

    def verify(self, request):
        self.requests.append(request)
        return observe_credential_delivery(Probe())


class Proposer:
    def propose(self, task):
        return ProposalRef(task.run_id, task.source_sha, "b" * 64)


class Lifecycle:
    def __init__(self):
        self.cleaned = []

    def create(self, run_id):
        return NAMESPACE

    def cleanup(self, namespace):
        self.cleaned.append(namespace)
        return True


class Isolation:
    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-1")


def test_runner_hands_credential_task_to_job_without_trusting_observations():
    task = prepare_credential_migration("run-1", REVISION)
    job, lifecycle = Job(), Lifecycle()
    result = run_once(task, Proposer(), lifecycle, Isolation(), CredentialVerifier(task, job))
    assert result.state == "RECEIPT_UNVERIFIED"
    assert result.untrusted_receipt == CredentialObservations("yes", "yes", "no", "yes")
    request = job.requests[0]
    assert (request.run_id, request.source_sha, request.proposal_digest, request.namespace) == (
        "run-1", REVISION, "b" * 64, NAMESPACE,
    )
    assert lifecycle.cleaned == [NAMESPACE]
    assert "password" not in repr(request)


@pytest.mark.parametrize("proposal,namespace", [
    (ProposalRef("other", REVISION, "b" * 64), NAMESPACE),
    (ProposalRef("run-1", "c" * 40, "b" * 64), NAMESPACE),
    (ProposalRef("run-1", REVISION, "invalid"), NAMESPACE),
    (ProposalRef("run-1", REVISION, "b" * 64), Namespace("default", "uid")),
    (ProposalRef("run-1", REVISION, "b" * 64), Namespace("rehearsal-credentials", "")),
])
def test_mismatched_credential_handoff_never_reaches_job(proposal, namespace):
    job = Job()
    task = prepare_credential_migration("run-1", REVISION)
    with pytest.raises(ValueError):
        CredentialVerifier(task, job).verify(proposal, namespace)
    assert job.requests == []


def test_invalid_credential_task_never_reaches_job():
    job = Job()
    task = replace(prepare_credential_migration("run-1", REVISION), source_sha="invalid")
    with pytest.raises(ValueError):
        CredentialVerifier(task, job).verify(ProposalRef("run-1", REVISION, "b" * 64), NAMESPACE)
    assert job.requests == []
