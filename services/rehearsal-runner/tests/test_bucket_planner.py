import base64
from types import SimpleNamespace

import pytest

from rehearsal_runner.attempt_store import AttemptStore, PlanBinding
from rehearsal_runner.bucket_job import BucketFixture, SeaweedBucketJob
from rehearsal_runner.bucket_planner import BucketPlanner, BucketTarget, build_bucket_host
from rehearsal_runner.bucket_proposer import T11BucketProposer
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.bucket_verifier import BucketVerifier
from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle
from rehearsal_runner.rehearsal_host import T15RehearsalHost
from rehearsal_runner.runner import T3Gate

FIXTURE = BucketFixture("http://seaweedfs.target-dev.svc:8333", "rehearsal-attachments", "canary",
                        "a" * 64, "docker.io/amazon/aws-cli@sha256:" + "b" * 64)
PROFILE = {"SEAWEEDFS_NAMESPACE": "target-dev", "SEAWEEDFS_LABEL_KEY": "app.kubernetes.io/name",
           "SEAWEEDFS_LABEL_VALUE": "target-seaweedfs", "SEAWEEDFS_PORT": "8333",
           "OPENBAO_NAMESPACE": "bravo-secrets", "OPENBAO_LABEL_KEY": "app.kubernetes.io/instance",
           "OPENBAO_LABEL_VALUE": "target-openbao", "OPENBAO_PORT": "8200"}
TASK = prepare_bucket_policy_task("run-1", '{"Statement":[]}', "rehearsal-attachments", source_sha="1" * 40)


class Kube:
    def get(self, kind, name, namespace=None):
        assert (kind, name, namespace) == ("secret", "rehearsal-object-admin", "target-dev")
        return {"data": {"access": base64.b64encode(b"AK").decode(), "secret": base64.b64encode(b"SK").decode()}}


class Proposals:
    def __init__(self):
        self.opened = []

    def open(self, admission, task):
        self.opened.append(task.run_id)
        return SimpleNamespace(snapshot=object()), object()


def planner(**changes):
    values = dict(profile=PROFILE, target=BucketTarget(FIXTURE, "target-dev", "rehearsal-object-admin"),
                  proposals=Proposals(), bindings=lambda _a: PlanBinding("c" * 64, "env-onprem"),
                  kubectl=object(), kube=Kube(), isolation_image="busybox:1.36")
    return BucketPlanner(**(values | changes))


def test_first_attempt_assembles_real_ports_for_the_admitted_task():
    p = planner()
    ports = p.factory(object(), TASK).prepare(1, None)
    assert isinstance(ports.proposer, T11BucketProposer) and ports.proposer.attempt == 1
    assert isinstance(ports.lifecycle, NamespaceLifecycle) and ports.lifecycle.attempt == 1
    assert ports.lifecycle.cleaner is p.kube
    assert isinstance(ports.isolation, T3Gate) and ports.isolation.cleanup_timeout == 30
    assert isinstance(ports.verifier, BucketVerifier) and ports.verifier.attempt == 1
    assert isinstance(ports.verifier.job, SeaweedBucketJob) and ports.verifier.job.fixture == FIXTURE
    assert p.proposals.opened == ["run-1"]
    assert p.binding(object()) == PlanBinding("c" * 64, "env-onprem")


def test_runner_reads_the_test_bucket_credential_only_when_verifying():
    assert planner().credential() == ("AK", "SK")


def test_rehearsal_cannot_be_reopened_or_resumed_from_another_process():
    p = planner()
    p.factory(object(), TASK).prepare(1, None)
    with pytest.raises(ValueError, match="already opened"):
        p.factory(object(), TASK).prepare(1, None)
    with pytest.raises(ValueError, match="not in this process"):
        planner().factory(object(), TASK).prepare(2, "d" * 64)


def test_host_overrides_are_applied():
    wrapped, cleaner = [], object()
    p = planner(cleaner=cleaner, isolation_cleanup_seconds=300,
                wrap_lifecycle=lambda inner: wrapped.append(inner) or inner)
    ports = p.factory(object(), TASK).prepare(1, None)
    assert wrapped == [ports.lifecycle] and ports.lifecycle.cleaner is cleaner
    assert ports.isolation.cleanup_timeout == 300


@pytest.mark.parametrize("namespace,secret", [("Target", "x"), ("target-dev", "bad_name")])
def test_credential_reference_must_be_a_kubernetes_name(namespace, secret):
    with pytest.raises(ValueError):
        BucketTarget(FIXTURE, namespace, secret)


def test_build_host_returns_t10_host_and_the_same_record_reader(tmp_path):
    store = AttemptStore(tmp_path, b"k" * 32)
    host, evidence = build_bucket_host(object(), planner(), store)
    assert isinstance(host, T15RehearsalHost) and host.evidence is evidence
    assert evidence.store is store and host.sender.store is store
