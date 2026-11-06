#!/usr/bin/env python3
"""Render the one-shot attachment copy Job and its S3 session policy; never contact anything.

The Job runs attachment_copy.py once in the target namespace. Credentials and private
identifiers stay Secret references, so the manifest reveals neither. The database route is
the existing replication contract. The operator verifies the egress Service, creates the
source Secret from a short session and starts the Job. The session comes from assuming the
infra/envs/attachment-copy-reader role (SSO users, the default) or from a federation token (IAM
users); both carry the same session policy. See docs/operations/t8-attachment-copy-job.md.
"""
import argparse
import json
import re
import uuid

from render import LABELS, NAMESPACE, resource
from replication import external_cluster

NAME_PREFIX = "attachment-copy-"
# The Job name is copied into a Pod label, whose limit is 63 characters.
MAX_RUN_ID_LENGTH = 63 - len(NAME_PREFIX)
RUN_LABEL = "bravo.t8/run-id"
EGRESS_UID_ANNOTATION = "bravo.t8/source-egress-service-uid"
PLAN_ANNOTATION = "bravo.t8/plan-id"
EPOCH_ANNOTATION = "bravo.t8/epoch"
CONTAINER = "copy"
RUN_AS = 10001

SOURCE_SECRET = "attachment-copy-source"
TARGET_SECRET = "target-object-client"
EXTRAS_SECRET = "attachment-copy-approved-extras"
TARGET_HOST = f"seaweedfs.{NAMESPACE}.svc.cluster.local"
TARGET_PORT = 8333
TARGET_ENDPOINT = f"http://{TARGET_HOST}:{TARGET_PORT}"
TARGET_BUCKET = "demo-chat-attachments"

DEADLINE_SECONDS = 600
# The session must outlive the Job, or a slow copy would fail on expired credentials instead of the deadline.
SOURCE_SESSION_SECONDS = 1800
# Both session commands in the guide use this name, so CloudTrail shows one name for the copy.
SOURCE_SESSION_NAME = "t8-attachment-copy"
# Long enough to read the receipt after an unattended run. Rerunning is safe if it is gone.
CLEANUP_SECONDS = 86400

# A traceback or a library warning on stderr can name an object key or a host. Only the sanitized
# receipt on stdout reaches the cluster's log store; a crash leaves an empty log, which reads as UNKNOWN.
COMMAND = ["/bin/sh", "-c", "exec python3 /app/attachment_copy.py 2>/dev/null"]

# On k3s, connections from a new labeled Pod to the object store were refused for about its first
# second, presumably until the NetworkPolicy admitted its address. The copy code reaches the store
# sooner than that and never retries (a lost PUT response would look like a conflict), so it failed
# every time. Wait here instead, in the same Pod and so with the same address. Never reaching the
# store leaves no receipt, which reads as UNKNOWN.
WAIT_FOR_TARGET = f"""\
import socket, sys, time
for _ in range(30):
    try:
        socket.create_connection(({TARGET_HOST!r}, {TARGET_PORT}), timeout=2).close()
        sys.exit(0)
    except OSError:
        time.sleep(2)
sys.exit(1)
"""

SOURCE_SESSION_ACTIONS = ["s3:ListBucket", "s3:GetObject", "kms:Decrypt"]


def validate_image(image):
    if not isinstance(image, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}", image):
        raise ValueError("image pinned by an exact sha256 digest required")


def validate_run_id(run_id):
    if (not isinstance(run_id, str) or len(run_id) > MAX_RUN_ID_LENGTH
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", run_id)):
        raise ValueError("run ID must be a short lowercase DNS label")


def validate_plan(plan_id, epoch):
    """The Cutover Executor's own request contract, so its plan and epoch can be compared exactly."""
    if not isinstance(plan_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", plan_id):
        raise ValueError("plan ID must match the executor's Identifier pattern")
    if type(epoch) is not int or epoch < 1:
        raise ValueError("epoch must be a positive integer")


def job_name(run_id):
    return NAME_PREFIX + run_id


def _secret_env(name, secret, key):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def job(image, run_id, plan_id, epoch, egress_address, egress_service_uid, *, with_approved_extras=False):
    validate_image(image)
    validate_run_id(run_id)
    validate_plan(plan_id, epoch)
    if not isinstance(egress_service_uid, str) or str(uuid.UUID(egress_service_uid)) != egress_service_uid:
        raise ValueError("exact egress Service UID required")
    # The route already allowed for chat_replicator: same host, certificate, Secret and tailnet tag.
    route = external_cluster("target", egress_address)
    database = route["connectionParameters"]
    ca = route["sslRootCert"]
    ca_directory = "/tls/source"
    env = [
        _secret_env("ATTACHMENT_SOURCE_BUCKET", SOURCE_SECRET, "bucket"),
        _secret_env("ATTACHMENT_SOURCE_REGION", SOURCE_SECRET, "region"),
        _secret_env("ATTACHMENT_SOURCE_BUCKET_OWNER", SOURCE_SECRET, "bucket_owner"),
        # A session token is required, so a long-lived key alone never starts this container.
        _secret_env("AWS_ACCESS_KEY_ID", SOURCE_SECRET, "aws_access_key_id"),
        _secret_env("AWS_SECRET_ACCESS_KEY", SOURCE_SECRET, "aws_secret_access_key"),
        _secret_env("AWS_SESSION_TOKEN", SOURCE_SECRET, "aws_session_token"),
        {"name": "ATTACHMENT_TARGET_ENDPOINT_URL", "value": TARGET_ENDPOINT},
        {"name": "ATTACHMENT_TARGET_BUCKET", "value": TARGET_BUCKET},
        _secret_env("ATTACHMENT_TARGET_ACCESS_KEY_ID", TARGET_SECRET, "AWS_ACCESS_KEY_ID"),
        _secret_env("ATTACHMENT_TARGET_SECRET_ACCESS_KEY", TARGET_SECRET, "AWS_SECRET_ACCESS_KEY"),
        {"name": "PGHOST", "value": database["host"]},
        {"name": "PGHOSTADDR", "value": database["hostaddr"]},
        {"name": "PGPORT", "value": database["port"]},
        {"name": "PGDATABASE", "value": database["dbname"]},
        {"name": "PGUSER", "value": database["user"]},
        _secret_env("PGPASSWORD", route["password"]["name"], route["password"]["key"]),
        {"name": "PGSSLMODE", "value": database["sslmode"]},
        {"name": "PGSSLROOTCERT", "value": f"{ca_directory}/{ca['key']}"},
        {"name": "PGAPPNAME", "value": "bravo_t8_attachment_copy"},
    ]
    volumes = [{"name": "source-ca", "secret": {"secretName": ca["name"],
                                                "items": [{"key": ca["key"], "path": ca["key"]}]}}]
    mounts = [{"name": "source-ca", "mountPath": ca_directory, "readOnly": True}]
    if with_approved_extras:
        # Object keys are private, so they stay in a Secret instead of the manifest.
        extras_directory = "/run/approved-extras"
        env.append({"name": "ATTACHMENT_APPROVED_EXTRA_KEYS_FILE", "value": f"{extras_directory}/keys"})
        volumes.append({"name": "approved-extras", "secret": {"secretName": EXTRAS_SECRET,
                                                              "items": [{"key": "keys", "path": "keys"}]}})
        mounts.append({"name": "approved-extras", "mountPath": extras_directory, "readOnly": True})

    container_security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                          "capabilities": {"drop": ["ALL"]}}
    selector = {"app.kubernetes.io/name": "attachment-copy", RUN_LABEL: run_id}
    result = resource("batch/v1", "Job", job_name(run_id), spec={
        "backoffLimit": 0, "parallelism": 1, "completions": 1,
        "activeDeadlineSeconds": DEADLINE_SECONDS, "ttlSecondsAfterFinished": CLEANUP_SECONDS,
        "template": {
            "metadata": {
                # The object store's NetworkPolicy admits only this label.
                "labels": {**LABELS, **selector, "bravo-object-client": "true"},
                "annotations": {EGRESS_UID_ANNOTATION: egress_service_uid, PLAN_ANNOTATION: plan_id,
                                EPOCH_ANNOTATION: str(epoch)},
            },
            "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                # Created once by render.py accounts; this Job is created per run and must not recreate it.
                "serviceAccountName": "attachment-copy",
                "securityContext": {"runAsNonRoot": True, "runAsUser": RUN_AS, "runAsGroup": RUN_AS,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "initContainers": [{
                    "name": "wait-for-target", "image": image, "imagePullPolicy": "IfNotPresent",
                    "command": ["python3", "-c", WAIT_FOR_TARGET], "securityContext": container_security,
                    "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                  "limits": {"cpu": "100m", "memory": "64Mi"}},
                }],
                "containers": [{
                    "name": CONTAINER, "image": image, "imagePullPolicy": "IfNotPresent",
                    "command": COMMAND, "env": env, "volumeMounts": mounts,
                    "securityContext": container_security,
                    # Peak was 108MB with 10MiB + 6MiB (docs/evidence/t8-attachment-sync-2026-11-04).
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                  "limits": {"cpu": "1", "memory": "256Mi"}},
                }],
                "volumes": volumes,
            },
        },
    })
    result["metadata"]["labels"].update(selector)
    return result


def source_session_policy(bucket_arn, kms_key_arn):
    """Session policy for AssumeRole or GetFederationToken: only list, read and S3-mediated decrypt.

    The reader role's own policy is the same document (infra/envs/attachment-copy-reader).
    """
    if not isinstance(bucket_arn, str) or not re.fullmatch(r"arn:aws:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket_arn):
        raise ValueError("exact S3 bucket ARN required")
    key = re.fullmatch(r"arn:aws:kms:([a-z0-9-]+):[0-9]{12}:key/[0-9a-f-]{36}", kms_key_arn) if isinstance(
        kms_key_arn, str) else None
    if key is None:
        raise ValueError("exact KMS key ARN required")
    objects = bucket_arn + "/*"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": bucket_arn},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": objects},
        {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": kms_key_arn,
         "Condition": {"StringEquals": {"kms:ViaService": f"s3.{key.group(1)}.amazonaws.com"}}},
        # Explicit denials also constrain grants that a resource policy might add.
        {"Effect": "Deny", "NotAction": SOURCE_SESSION_ACTIONS, "Resource": "*"},
        {"Effect": "Deny", "Action": ["s3:ListBucket", "s3:GetObject"], "NotResource": [bucket_arn, objects]},
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest="stage", required=True)
    job_stage = stages.add_parser("job")
    job_stage.add_argument("--image", required=True)
    job_stage.add_argument("--run-id", required=True)
    job_stage.add_argument("--plan-id", required=True)
    job_stage.add_argument("--epoch", required=True, type=int)
    job_stage.add_argument("--egress-address", required=True)
    job_stage.add_argument("--egress-service-uid", required=True)
    job_stage.add_argument("--with-approved-extras", action="store_true")
    policy_stage = stages.add_parser("source-policy")
    policy_stage.add_argument("--bucket-arn", required=True)
    policy_stage.add_argument("--kms-key-arn", required=True)
    args = parser.parse_args()
    try:
        if args.stage == "job":
            rendered = job(args.image, args.run_id, args.plan_id, args.epoch, args.egress_address,
                           args.egress_service_uid,
                           with_approved_extras=args.with_approved_extras)
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [rendered]}, indent=2))
        else:
            print(json.dumps(source_session_policy(args.bucket_arn, args.kms_key_arn), separators=(",", ":")))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
