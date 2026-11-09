import hashlib
import io
from dataclasses import replace

import pytest

from rehearsal_runner.bucket_probe import ReadResult, observe_bucket_reads
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.bucket_verifier import BucketVerifier
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef, run_once

SOURCE = '{"Statement": []}'
REVISION = "a" * 40
CONTENT = b"test canary"
NAMESPACE = Namespace("rehearsal-bucket", "uid-original")


def task():
    return prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha=REVISION)


class Reader:
    def __init__(self, status):
        self.status = status

    def get(self, bucket, key):
        assert (bucket, key) == ("rehearsal-test", "canary")
        return ReadResult(self.status, io.BytesIO(CONTENT) if self.status == 200 else None)


class MockJob:
    def __init__(self, anonymous_status):
        self.requests = []
        self.anonymous_status = anonymous_status

    def verify(self, request):
        self.requests.append(request)
        return observe_bucket_reads(
            Reader(200), Reader(self.anonymous_status), bucket=request.target_bucket,
            key="canary", expected_sha256=hashlib.sha256(CONTENT).hexdigest(),
        )


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


@pytest.mark.parametrize("status,anonymous", [(200, "allowed"), (403, "denied"), (500, "unknown")])
def test_runner_binds_bucket_job_and_keeps_observation_untrusted(status, anonymous):
    bucket_task = task()
    job, lifecycle = MockJob(status), Lifecycle()
    result = run_once(bucket_task, Proposer(), lifecycle, Isolation(), BucketVerifier(bucket_task, job))
    assert result.state == "RECEIPT_UNVERIFIED"
    assert result.untrusted_receipt.anonymous == anonymous
    assert result.untrusted_receipt.authenticated_before == "matches"
    assert result.untrusted_receipt.authenticated_after == "matches"
    request = job.requests[0]
    assert (request.run_id, request.source_sha, request.source_policy_sha256) == (
        bucket_task.run_id, bucket_task.source_sha, bucket_task.source_sha256,
    )
    assert (request.proposal_digest, request.target_bucket, request.namespace) == (
        "b" * 64, "rehearsal-test", NAMESPACE,
    )
    assert lifecycle.cleaned == [NAMESPACE]


@pytest.mark.parametrize("proposal,namespace", [
    (ProposalRef("other", REVISION, "b" * 64), NAMESPACE),
    (ProposalRef("run-1", "c" * 40, "b" * 64), NAMESPACE),
    (ProposalRef("run-1", REVISION, "invalid"), NAMESPACE),
    (ProposalRef("run-1", REVISION, "b" * 64), Namespace("default", "uid")),
    (ProposalRef("run-1", REVISION, "b" * 64), Namespace("rehearsal-bucket", "")),
])
def test_mismatched_binding_never_reaches_job(proposal, namespace):
    job = MockJob(403)
    with pytest.raises(ValueError):
        BucketVerifier(task(), job).verify(proposal, namespace)
    assert job.requests == []


def test_forged_task_digest_never_reaches_job():
    job = MockJob(403)
    forged = replace(task(), source_sha256="0" * 64)
    with pytest.raises(ValueError):
        BucketVerifier(forged, job).verify(ProposalRef("run-1", REVISION, "b" * 64), NAMESPACE)
    assert job.requests == []
