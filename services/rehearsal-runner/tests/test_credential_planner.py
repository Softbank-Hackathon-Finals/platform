from types import SimpleNamespace

import pytest

from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.credential_job import CredentialFixture, CredentialJob
from rehearsal_runner.credential_planner import CredentialPlanner, RoutedPlanner
from rehearsal_runner.credential_verifier import CredentialVerifier
from rehearsal_runner.secret_task import prepare_credential_migration

TASK = prepare_credential_migration("plan1-1", "1" * 40)
PROFILE = {f"{p}_{k}": v for p in ("SEAWEEDFS", "OPENBAO")
           for k, v in (("NAMESPACE", "target-dev"), ("LABEL_KEY", "app"), ("LABEL_VALUE", "x"), ("PORT", "8333"))}
IMAGE = "registry.test/x@sha256:" + "a" * 64


class Proposals:
    def __init__(self):
        self.opened = []

    def open(self, admission, task):
        self.opened.append(task.run_id)
        return SimpleNamespace(snapshot=object()), object()


def planner(proposals=None):
    fixture = CredentialFixture(bao_image=IMAGE, postgres_image=IMAGE, probe_image=IMAGE,
                                openbao_ca_pem="-----BEGIN CERTIFICATE-----\n")
    return CredentialPlanner(PROFILE, proposals or Proposals(), lambda a: "binding", object(), object(), IMAGE,
                             fixture, object(), lambda diff_hash: IMAGE, job_deadline_seconds=90)


def test_first_attempt_opens_the_proposal_once_and_wires_the_credential_job():
    proposals = Proposals()
    p = planner(proposals)
    ports = p.factory(None, TASK).prepare(1, None)
    assert proposals.opened == ["plan1-1"]
    assert isinstance(ports.verifier, CredentialVerifier) and isinstance(ports.verifier.job, CredentialJob)
    assert ports.verifier.job.deadline_seconds == 90 and ports.verifier.attempt == 1
    with pytest.raises(ValueError, match="already opened"):
        p.factory(None, TASK).prepare(1, None)


def test_later_attempt_after_restart_stops_before_any_effect():
    with pytest.raises(ValueError, match="not in this process"):
        planner().factory(None, TASK).prepare(2, "f" * 64)


def test_non_credential_task_is_refused():
    bucket = prepare_bucket_policy_task("plan1-1", '{"Statement":[]}', "rehearsal-attachments", source_sha="1" * 40)
    with pytest.raises(ValueError, match="credential task required"):
        planner().factory(None, bucket)


def test_routed_planner_sends_each_task_kind_to_its_planner():
    bucket = SimpleNamespace(binding=lambda a: "bucket-binding", factory=lambda a, t: "bucket-factory")
    routed = RoutedPlanner(bucket, SimpleNamespace(factory=lambda a, t: "credential-factory"))
    assert routed.factory(None, TASK) == "credential-factory"
    assert routed.factory(None, object()) == "bucket-factory"
    assert routed.binding(None) == "bucket-binding"
