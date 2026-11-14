"""`target.rehearsal` and `target.neutral_apply` (secrets): choosing by target kind."""

import json

import pytest
from neutral_fixtures import APP, BUCKET, SOURCES

from rehearsal_runner import adapters
from rehearsal_runner.bucket_job import (
    BucketFixture,
    GcsBucketJob,
    S3BucketJob,
    SeaweedBucketJob,
    bucket_job_class,
)
from rehearsal_runner.bucket_proposer import bucket_summary
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.credential_job import CredentialJob
from rehearsal_runner.credential_targets import OPENBAO, credential_target
from rehearsal_runner.isolation.profile import targets
from rehearsal_runner.neutral import (
    UNSUPPORTED,
    NeutralError,
    SecretBinding,
    SourceRef,
    Unsupported,
    Workload,
    render_openbao_delivery,
    render_secret,
)
from rehearsal_runner.rehearsal_policy import control_policy
from rehearsal_runner.secret_task import prepare_credential_migration

NS = "rehearsal-abc123"
META = {"namespace": NS, "labels": {"bravo.dev/run": "r1"}, "annotations": {"bravo.dev/attempt": "1"}}
IMAGE = "registry.example/cli@sha256:" + "c" * 64
BINDING = SecretBinding("demo-chat-db", SourceRef("aws", "prod/demo-chat/db"), ("username", "password"),
                        (Workload(NS, "demo-chat"),))
PROFILE = {f"{p}_{k}": v for p in ("SEAWEEDFS", "OPENBAO") for k, v in
           (("NAMESPACE", "target-dev"), ("LABEL_KEY", "app"), ("LABEL_VALUE", p.lower()), ("PORT", "8333"))}


def test_job_classes_and_credential_targets_are_chosen_by_target_kind():
    assert bucket_job_class("onprem") is SeaweedBucketJob
    assert bucket_job_class("aws") is S3BucketJob and bucket_job_class("gcp") is GcsBucketJob
    assert bucket_job_class("azure") == UNSUPPORTED and credential_target("azure") == UNSUPPORTED
    assert credential_target("onprem") is OPENBAO and OPENBAO.job_class() is CredentialJob
    assert OPENBAO.store_address == "https://target-openbao.bravo-secrets.svc:8200"  # unchanged constants


def test_isolation_profile_is_onprem_only_until_cloud_egress_is_checked():
    assert [t.name for t in targets(PROFILE)] == ["seaweedfs", "openbao"]
    for kind in ("aws", "gcp"):
        with pytest.raises(Unsupported, match="adapter_missing:target.rehearsal"):
            targets(PROFILE | {"TARGET_KIND": kind})


@pytest.mark.parametrize("source", ["aws", "gcp", "onprem"])
@pytest.mark.parametrize("target", ["aws", "gcp", "onprem"])
def test_readiness_names_the_missing_slot(source, target):
    ready = adapters.readiness(source, target)
    assert ready["source.neutral_export"] == ready["target.neutral_apply"] == "READY"
    expected = "READY" if target == "onprem" else UNSUPPORTED
    assert ready["target.rehearsal"] == expected
    assert adapters.rejections(source, target) == ([] if target == "onprem" else ["adapter_missing:target.rehearsal"])
    assert adapters.readiness("aws", "azure")["target.neutral_apply"] == UNSUPPORTED


@pytest.mark.parametrize("kind, cls, cli, absent", [
    ("aws", S3BucketJob, "aws s3api put-bucket-policy", "--endpoint-url"),
    ("gcp", GcsBucketJob, "gcloud storage buckets set-iam-policy", "aws "),
])
def test_cloud_bucket_jobs_apply_the_render_without_static_keys(kind, cls, cli, absent):
    fixture = BucketFixture("https://storage.googleapis.com:443", "rehearsal-test", "canary.txt", "a" * 64, IMAGE)
    created = []

    class Kube:
        def create(self, manifest):
            created.append(manifest)
            return manifest

    task = prepare_bucket_policy_task("run-1", SOURCES["aws"], "rehearsal-test", source_sha="a" * 40,
                                      target_kind=kind)
    job = cls(Kube(), None, None, fixture, None, control=control_policy(task))
    job._auth_objects(META)
    assert created == []  # no Secret: the service account's cloud identity is the credential
    config = job.policies_config_map(META, SOURCES["aws"], "{}")
    control = json.loads(config["data"]["source.json"])
    assert (control.get("Statement") if kind == "aws" else control.get("bindings"))
    manifest = job._job(None, "bucket-control", "source.json", META)
    pod = manifest["spec"]["template"]["spec"]
    script = pod["containers"][0]["command"][-1]
    assert cli in script and absent not in script
    assert pod["serviceAccountName"] == "rehearsal-bucket-admin" and pod["automountServiceAccountToken"] is False
    assert not any("secretKeyRef" in json.dumps(e) for e in pod["containers"][0]["env"])
    with pytest.raises(ValueError):
        cls(Kube(), None, None, fixture, None, service_account="default")


def test_control_refuses_a_changed_source():
    task = prepare_bucket_policy_task("run-1", SOURCES["gcp"], "rehearsal-test", source_sha="a" * 40,
                                      source_kind="gcp", target_kind="onprem")
    control = control_policy(task)
    assert json.loads(control(SOURCES["gcp"]))["Statement"]
    with pytest.raises(ValueError):
        control(SOURCES["aws"])


def test_non_default_pairs_name_their_kinds_to_the_model():
    task = prepare_bucket_policy_task("run-1", SOURCES["onprem"], "rehearsal-test", source_sha="a" * 40,
                                      source_kind="onprem", target_kind="gcp")
    assert task.target_service == "gcs"
    assert task.as_model_input()["source_kind"] == "onprem"
    assert bucket_summary(task, "s.json", "t.json").startswith(
        "Translate the SeaweedFS s3.json identities in s.json for GCS test bucket rehearsal-test;")
    cred = prepare_credential_migration("run-1", "a" * 40, source_kind="gcp", target_kind="aws")
    assert cred.as_model_input()["objective"].startswith("Replace direct GCP Secret Manager reads")
    with pytest.raises(ValueError):
        prepare_bucket_policy_task("run-1", SOURCES["aws"], "rehearsal-test", source_sha="a" * 40, source_kind="gcp")
    with pytest.raises(Unsupported):
        prepare_bucket_policy_task("run-1", SOURCES["aws"], "rehearsal-test", source_sha="a" * 40, source_kind="azure")


# --- secrets: neutral → target ----------------------------------------------------------------------

def test_openbao_render_uses_the_t9_delivery():
    class Delivery:
        def remote_path(self, ns): return f"rehearsal/{ns}/database"
        def role_name(self, ns): return f"rehearsal-{ns}"
        def auth_role(self, ns): return {"bound_service_account_namespaces": [ns]}
        def init_container(self, ns): return {"name": "fetch"}

    rendered = render_openbao_delivery(BINDING, delivery=Delivery(), namespace=NS)
    assert rendered.document["remote_path"] == f"rehearsal/{NS}/database"
    assert rendered.document["service_accounts"] == ["demo-chat"]
    with pytest.raises(NeutralError):
        render_openbao_delivery(BINDING, delivery=Delivery(), namespace="rehearsal-other")


@pytest.mark.parametrize("kind, context, provider", [
    ("aws", {"region": "us-east-1"}, "aws"),
    ("gcp", {"project_id": "example-project", "cluster_location": "us-central1", "cluster_name": "target"}, "gcpsm"),
])
def test_eso_render_and_rehearsal_jobs(kind, context, provider):
    target = credential_target(kind)
    assert target.runnable is False
    store, external = render_secret(kind, BINDING, namespace=NS, remote_key="k", store_service_account="reader",
                                    **context).document
    assert provider in store["spec"]["provider"]
    assert [d["secretKey"] for d in external["spec"]["data"]] == ["username", "password"]
    assert external["spec"]["target"]["name"] == "demo-chat-db"
    manifests = target.rehearsal_manifests(BINDING, NS, META, **context)
    assert [m["kind"] for m in manifests] == ["ServiceAccount", "SecretStore", "ExternalSecret"]
    assert all(m["metadata"]["namespace"] == NS and m["metadata"]["labels"] == META["labels"] for m in manifests)
    remote = manifests[2]["spec"]["data"][0]["remoteRef"]["key"]
    assert NS in remote and remote == target.remote_key(NS, BINDING)
    job_context = {"region": "us-east-1"} if kind == "aws" else {"project_id": "example-project"}
    writer = target.writer_job(BINDING, NS, META, image=IMAGE, work_secret="t15-cred-x", **job_context)
    cleanup = target.cleanup_job(BINDING, NS, META, image=IMAGE, **job_context)
    for job in (writer, cleanup):
        assert job["metadata"]["namespace"] == "bravo-rehearsal-control"
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["REMOTE"] == remote
    assert "synthetic.json" in writer["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "/work" not in json.dumps(cleanup["spec"]["template"]["spec"]["volumes"])
    with pytest.raises(ValueError):
        target.remote_key("default", BINDING)


def test_secret_render_without_a_consumer_in_the_namespace_is_refused():
    other = SecretBinding("db", SourceRef("gcp", "db"), ("password",), (APP,))
    with pytest.raises(NeutralError, match="NO_CONSUMER_IN_NAMESPACE"):
        render_secret("aws", other, namespace=NS, region="us-east-1", remote_key="k", store_service_account="r")
    assert BUCKET  # fixtures module shared with the matrix tests
