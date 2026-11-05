import hashlib
import itertools
import json
from types import SimpleNamespace

import pytest

from rehearsal_runner.credential_job import (
    CONTROL_NAMESPACE,
    CredentialFixture,
    CredentialJob,
    VerificationUnknown,
    judge,
)
from rehearsal_runner.credential_proposal_source import ProposedEdit
from rehearsal_runner.credential_verifier import CredentialVerificationRequest
from rehearsal_runner.runner import Namespace

NS = "rehearsal-plan1-1"
SECRET = "synthetic-pw-not-for-logs"
CANDIDATE, BAO, PG, PROBE = (f"registry.test/{n}@sha256:" + c * 64 for n, c in (("app", "a"), ("bao", "b"), ("pg", "c"), ("probe", "d")))
BEFORE = "import boto3\n"
OK = {"writer": {"login": "allow", "value": "ok", "role": "ok"}, "db_ready": "yes", "secret_before": "yes",
      "secret_after": "yes", "init": "succeeded", "app": "ready", "app_db": "yes",
      "intruder": {"reach": "yes", "login": "denied"}, "intruder_secret_read": "denied"}
LOGS = {"t15-cred-rehearsal-plan1-1-write": {"login": "allow", "value": "ok", "role": "ok"},
        "cred-app-db": {"observation": "yes"}, "cred-intruder": {"reach": "yes", "login": "denied"},
        "t15-cred-rehearsal-plan1-1-cleanup": {"login": "allow", "role": "deleted", "value": "deleted"}}


class Kube:
    def __init__(self, logs=None, app="ready"):
        self.logs, self.app, self.objects, self.deleted = dict(LOGS, **(logs or {})), app, {}, []
        self.uids = (f"uid-{n}" for n in itertools.count())

    def create(self, obj):
        obj = json.loads(json.dumps(obj))
        obj["metadata"]["uid"] = next(self.uids)
        self.objects[(obj["kind"].lower(), obj["metadata"]["name"])] = obj
        return obj

    def get(self, kind, name, namespace=None):
        if kind == "namespace":
            return {"metadata": {"uid": "ns-uid"}}
        obj = self.objects[(kind, name)]
        if kind == "job":
            obj["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        if kind == "pod":
            image = obj["spec"]["containers"][0]["image"]
            run = {"ready": True, "imageID": image, "restartCount": 0, "state": {"running": {}}, "containerID": "c1"}
            if name == "rehearsal-app" and self.app == "crashed":
                run = {"restartCount": 1, "state": {"terminated": {"exitCode": 1}}}
            obj["status"] = {"containerStatuses": [run],
                             "initContainerStatuses": [{"state": {"terminated": {"exitCode": 0}}}]}
        return obj

    def call(self, *args, data=None):
        if args[:2] == ("get", "pods"):
            job = next(o for o in self.objects.values() if o["kind"] == "Job"
                       and args[5].endswith(o["metadata"]["uid"]))
            image = job["spec"]["template"]["spec"]["containers"][0]["image"]
            return json.dumps({"items": [{
                "metadata": {"name": job["metadata"]["name"] + "-pod", "uid": "p-" + job["metadata"]["uid"],
                             "namespace": job["metadata"]["namespace"],
                             "ownerReferences": [{"controller": True, "uid": job["metadata"]["uid"], "kind": "Job",
                                                  "name": job["metadata"]["name"], "apiVersion": "batch/v1"}]},
                "spec": {"containers": [{"name": "run", "image": image}]},
                "status": {"phase": "Succeeded", "containerStatuses": [{
                    "name": "run", "imageID": image, "restartCount": 0, "containerID": "c-" + job["metadata"]["uid"],
                    "state": {"terminated": {"exitCode": 0, "containerID": "c-" + job["metadata"]["uid"],
                                             "finishedAt": "t"}}}]}}]})
        if args[0] == "logs":
            return json.dumps(self.logs[args[1].removeprefix("pod/").removesuffix("-pod")]) + "\n"
        if args[:2] == ("delete", "--raw"):
            self.deleted.append((args[2], data["preconditions"]["uid"]))
            return ""
        raise AssertionError(args)


class Kubectl:
    def __init__(self, identity=None, stderr=None):
        self.account = f"system:serviceaccount:{NS}:rehearsal-intruder"
        self.identity = identity or self.account
        self.stderr = stderr or f'Error from server (Forbidden): secrets is forbidden: User "{self.account}" cannot get'

    def run(self, *args):
        if args[:2] == ("auth", "whoami"):
            return SimpleNamespace(returncode=0, stdout=self.identity, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=self.stderr)


class Delivery:
    def remote_path(self, ns): return f"bravo/rehearsal/{ns}/database"
    def role_name(self, ns): return f"rehearsal-app-reader-{ns}"
    def auth_role(self, ns): return {"bound_service_account_namespaces": ns, "token_policies": "rehearsal-app-reader"}
    def init_container(self, ns): return {"name": "openbao-fetch", "image": BAO, "x-bravo-remote-path": "p"}


class Snapshot:
    def read(self, path):
        return BEFORE


class Proposals:
    def resolve(self, request):
        return (ProposedEdit("app/db_secret.py", hashlib.sha256(BEFORE.encode()).hexdigest(), "import json\n"),)


def make(kube=None, kubectl=None, image=CANDIDATE):
    fixture = CredentialFixture(bao_image=BAO, postgres_image=PG, probe_image=PROBE,
                                openbao_ca_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    return CredentialJob(kube or Kube(), kubectl or Kubectl(), Proposals(), Snapshot(), Delivery(), fixture,
                         lambda diff_hash: image, poll_seconds=0, deadline_seconds=1, password=lambda: SECRET,
                         observe=observe)


def observe(kube, created, image, *, complete):
    """Stand-in for T38 observe_job_pod: the Job's one pod and its image."""
    pod = json.loads(kube.call("get", "pods", "-n", created["metadata"]["namespace"], "-l",
                               "batch.kubernetes.io/controller-uid=" + created["metadata"]["uid"], "-o", "json"))["items"][0]
    status = pod["status"]["containerStatuses"][0]
    assert status["imageID"] == image and complete is True
    return {"job_uid": created["metadata"]["uid"], "pod_name": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
            "container_id": status["containerID"], "image_id": status["imageID"], "exit_code": 0}


REQUEST = CredentialVerificationRequest("plan1-1", "1" * 40, "e" * 64, Namespace(NS, "ns-uid"))


def test_injected_credentials_pass_and_openbao_is_cleaned_up():
    kube = Kube()
    evidence = make(kube).verify(REQUEST)
    assert (evidence.verdict, evidence.reason) == ("PASS", "NONE")
    assert evidence.image_digest == "sha256:" + "a" * 64
    assert evidence.observations["cleanup"] == {"login": "allow", "role": "deleted", "value": "deleted"}
    deleted = {path.rsplit("/", 1)[1] for path, _ in kube.deleted}
    assert {"t15-cred-rehearsal-plan1-1", "t15-cred-rehearsal-plan1-1-ca",
            "t15-cred-rehearsal-plan1-1-write", "t15-cred-rehearsal-plan1-1-cleanup"} <= deleted
    assert all(f"/namespaces/{CONTROL_NAMESPACE}/" in path for path, _ in kube.deleted)


def test_synthetic_password_lives_only_in_the_two_secrets():
    kube = Kube()
    evidence = make(kube).verify(REQUEST)
    assert SECRET not in json.dumps(evidence.public())
    holders = {name for (kind, name), obj in kube.objects.items() if SECRET in json.dumps(obj)}
    assert holders == {"t15-cred-rehearsal-plan1-1", "rehearsal-db-auth"}
    assert all(kind == "secret" for (kind, name) in kube.objects if name in holders)


def test_app_pod_uses_t9_init_container_and_the_three_volumes():
    kube = Kube()
    make(kube).verify(REQUEST)
    app = kube.objects[("pod", "rehearsal-app")]["spec"]
    assert app["serviceAccountName"] == "rehearsal-eso-reader" and app["automountServiceAccountToken"] is False
    assert [c["name"] for c in app["initContainers"]] == ["openbao-fetch"]
    assert not any(k.startswith("x-") for k in app["initContainers"][0])
    assert {v["name"] for v in app["volumes"]} == {"bravo-database", "openbao-ca", "openbao-token", "tmp"}
    assert app["securityContext"]["runAsUser"] == 10001
    assert app["containers"][0]["ports"] == [{"containerPort": 8080}]
    token = next(v for v in app["volumes"] if v["name"] == "openbao-token")
    assert token["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "openbao"
    writer = kube.objects[("job", "t15-cred-rehearsal-plan1-1-write")]
    assert writer["metadata"]["namespace"] == CONTROL_NAMESPACE
    pod = writer["spec"]["template"]
    assert pod["spec"]["serviceAccountName"] == "rehearsal-openbao-writer"
    assert pod["metadata"]["labels"]["app.kubernetes.io/component"] == "rehearsal-openbao-writer"


@pytest.mark.parametrize("kube,reason", [
    (Kube(app="crashed"), "APP_DB_CONNECTION_FAILED"),
    (Kube(logs={"cred-app-db": {"observation": "no"}}), "APP_DB_CONNECTION_FAILED"),
])
def test_candidate_that_cannot_reach_the_db_fails(kube, reason):
    evidence = make(kube).verify(REQUEST)
    assert (evidence.verdict, evidence.reason) == ("FAIL", reason)


@pytest.mark.parametrize("kube,kubectl,reason", [
    (Kube(logs={"t15-cred-rehearsal-plan1-1-write": {"login": "deny", "value": "failed", "role": "failed"}}),
     None, "writer_unconfirmed"),
    (Kube(logs={"cred-intruder": {"reach": "yes", "login": "allowed"}}), None, "isolation_unconfirmed"),
    (Kube(logs={"cred-intruder": {"reach": "no", "login": "unknown"}}), None, "isolation_unconfirmed"),
    (Kube(), Kubectl(identity="person@example.test"), "isolation_unconfirmed"),
    (Kube(), Kubectl(stderr='Error from server (Forbidden): User "person@example.test"'), "isolation_unconfirmed"),
    (Kube(logs={"t15-cred-rehearsal-plan1-1-cleanup": {"login": "allow", "role": "failed", "value": "deleted"}}),
     None, "openbao_cleanup_unconfirmed"),
])
def test_environment_problems_are_not_verdicts_and_still_clean_up(kube, kubectl, reason):
    with pytest.raises(VerificationUnknown, match=reason):
        make(kube, kubectl).verify(REQUEST)
    assert ("job", "t15-cred-rehearsal-plan1-1-cleanup") in kube.objects


def test_unpublished_candidate_stops_before_any_object():
    kube = Kube()
    with pytest.raises(VerificationUnknown, match="candidate_image_unpublished"):
        make(kube, image="demo-chat:latest").verify(REQUEST)
    assert kube.objects == {}


def test_judge_requires_every_control():
    assert judge(OK) == ("PASS", "NONE")
    for key, value in [("db_ready", "no"), ("secret_after", "unknown"), ("init", "failed"), ("app", "unknown")]:
        with pytest.raises(VerificationUnknown):
            judge(OK | {key: value})
