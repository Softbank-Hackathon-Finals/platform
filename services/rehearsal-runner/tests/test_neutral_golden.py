"""Golden: the existing aws → onprem outputs are unchanged by the export + apply split.

Byte-identical: model inputs, summaries, task digests and the SeaweedFS Job manifest (values
captured from the code before ADR-0034). Same meaning: the neutral path renders exactly the
``s3.json`` identities T9's renderer produces for the demo's two exposure states.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.bucket_job import BucketFixture, SeaweedBucketJob
from rehearsal_runner.bucket_proposer import bucket_summary
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.credential_proposer import credential_summary
from rehearsal_runner.neutral import (
    export_s3_bucket_policy,
    render_s3_bucket_policy,
    render_seaweedfs_identities,
)
from rehearsal_runner.neutral.export import S3_ARN
from rehearsal_runner.rehearsal_policy import control_policy, task_translation
from rehearsal_runner.secret_task import prepare_credential_migration

ROOT = Path(__file__).resolve().parents[3]
APP_BUCKET, REHEARSAL_BUCKET = "demo-chat-attachments", "rehearsal-test"
SOURCE = ('{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject",'
          '"Resource":"arn:' 'aws:s3:::demo-chat-attachments/*"}]}')  # split for the evidence scan
# The approved shape apply-path accepts (README "버킷 정책"): deny anonymous reads only.
APPROVED_CLOSED = json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject", "Resource": f"{S3_ARN}{REHEARSAL_BUCKET}/*",
    "Condition": {"StringEquals": {"aws:PrincipalType": "Anonymous"}}}]})
FAKE_ENV = {"BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY": "FAKEBOOTSTRAP00001", "BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY": "fake-bootstrap-" + "0" * 30,
            "BRAVO_OBJECT_CLIENT_ACCESS_KEY": "FAKECLIENT00000001", "BRAVO_OBJECT_CLIENT_SECRET_KEY": "fake-client-" + "1" * 30,
            "BRAVO_OBJECT_REHEARSAL_ACCESS_KEY": "FAKEREHEARSAL00001", "BRAVO_OBJECT_REHEARSAL_SECRET_KEY": "fake-rehearsal-" + "2" * 30}


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def test_bucket_task_model_input_summary_and_digest_are_byte_identical():
    task = prepare_bucket_policy_task("run-1", SOURCE, REHEARSAL_BUCKET, source_sha="a" * 40)
    assert bucket_summary(task, "src/source-policy.json", "src/target-policy.json") == (
        "Translate the S3 policy in src/source-policy.json for SeaweedFS test bucket rehearsal-test; "
        "propose changes only to src/target-policy.json. Deny anonymous reads and destructive access. "
        "Return a bounded policy change proposal.")
    assert _sha(task.as_model_input()) == "3d266a56b683a5402d451e184e5d564eb220eb20143648bd9c05487cf024c0ae"
    assert logical_task_digest(task) == "fdcb826f3cd1ddf0ee3dc7a65d819708fb97a50918298f0d406b880d47748d0b"


def test_credential_task_model_input_summary_and_digest_are_byte_identical():
    task = prepare_credential_migration("run-1", "a" * 40)
    assert credential_summary(task) == (
        "Replace direct Secrets Manager credential reads with ESO-injected /run/bravo/database/credentials.json, "
        "preserving cache and authentication-failure refresh. Edit only app/db_secret.py, app/config.py, and "
        "app/main.py. Fail closed on missing or invalid credentials; never log credentials.")
    assert _sha(task.as_model_input()) == "bfcf52389feef374b608f24dfeb51793ba6ad17b8a7bad8fec99e3d80d0cd862"
    assert logical_task_digest(task) == "2853fc081bb945f86173d4d65bd3f25bbd3827ff584fcd0b86efab7f0b2a3db2"


def test_seaweedfs_job_manifest_and_control_policy_are_byte_identical():
    fixture = BucketFixture("http://seaweedfs.target-dev.svc:8333", REHEARSAL_BUCKET, "canary.txt", "a" * 64,
                            "registry.example/aws-cli@sha256:" + "b" * 64)
    task = prepare_bucket_policy_task("run-1", SOURCE, REHEARSAL_BUCKET, source_sha="a" * 40)
    job = SeaweedBucketJob(None, None, None, fixture, lambda: ("x", "y"), control=control_policy(task))
    meta = {"namespace": "rehearsal-x", "labels": {}, "annotations": {}}
    assert _sha(job._job(None, "bucket-control", "source.json", meta)) == (
        "14cec1c08d6d91d95c76da770d8d57eab1408cf7e5c8cf62f041776f5f131c51")
    assert control_policy(task) is None
    assert job.policies_config_map(meta, SOURCE, "{}")["data"]["source.json"] == SOURCE


def test_rendered_control_means_the_same_as_the_raw_source():
    """aws → onprem keeps the raw source as control; the neutral render is its exact equivalent."""
    task = prepare_bucket_policy_task("run-1", SOURCE, REHEARSAL_BUCKET, source_sha="a" * 40)
    rendered = task_translation(task).rendered
    raw = export_s3_bucket_policy(SOURCE, bucket=APP_BUCKET)
    again = export_s3_bucket_policy(rendered.text(), bucket=REHEARSAL_BUCKET)
    assert (again.grants, again.public_read, again.public_list) == (raw.grants, raw.public_read, raw.public_list)


@pytest.fixture(scope="module")
def t9():
    path = ROOT / "infra/envs/target-workloads/object_exposure.py"
    spec = importlib.util.spec_from_file_location("t9_object_exposure_golden", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_closing_matches_t9_closed_state(t9):
    """Approved anonymous-deny policy → neutral → s3.json on the open store = T9 ``closed``."""
    opened = t9.render("open-demo", APP_BUCKET, FAKE_ENV, acknowledge_open=True, demo_run_id="demo-0001",
                       rehearsal_bucket=REHEARSAL_BUCKET)
    closed = t9.render("closed", APP_BUCKET, FAKE_ENV, rehearsal_bucket=REHEARSAL_BUCKET)
    neutral = export_s3_bucket_policy(APPROVED_CLOSED)
    rendered = render_seaweedfs_identities(neutral, base=opened, bucket=APP_BUCKET)
    assert rendered.document == closed  # byte-identical document, credentials carried over untouched
    assert rendered.text() == json.dumps(closed, indent=2) + "\n"


def test_opening_matches_t9_open_demo_identities(t9):
    """The demo source (public read) → neutral → s3.json on the closed store = T9 ``open-demo``."""
    opened = t9.render("open-demo", APP_BUCKET, FAKE_ENV, acknowledge_open=True, demo_run_id="demo-0001",
                       rehearsal_bucket=REHEARSAL_BUCKET)
    closed = t9.render("closed", APP_BUCKET, FAKE_ENV, rehearsal_bucket=REHEARSAL_BUCKET)
    rendered = render_seaweedfs_identities(export_s3_bucket_policy(SOURCE), base=closed)
    by_name = lambda doc: {i["name"]: i for i in doc["identities"]}  # noqa: E731 - order is not meaning
    assert by_name(rendered.document) == by_name(opened)


def test_s3_render_of_the_closed_state_is_the_approved_shape():
    """Rendering the neutral closed state for the rehearsal bucket gives the statement apply-path accepts."""
    rendered = render_s3_bucket_policy(export_s3_bucket_policy(APPROVED_CLOSED))
    statements = rendered.document["Statement"]
    assert [{k: v for k, v in s.items() if k != "Sid"} for s in statements] == json.loads(APPROVED_CLOSED)["Statement"]
