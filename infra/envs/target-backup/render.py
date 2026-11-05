#!/usr/bin/env python3
"""Render T35 target (on-prem SeaweedFS) backup registration and CNPG manifests.

Run from the repository root with `uv run --project services/control-runner`. Prints JSON:
identities and a bucket policy for the SeaweedFS owner (T9 · T30) to merge, or Kubernetes
objects and merge patches for the target Cluster. Rendering is not apply and never emits keys.
"""

import argparse
import json
import re
import sys
from string import Template

from bravo_controls.cnpg_backup import (
    archive_patch,
    backup_request,
    object_store,
    restore_cluster,
)

NAMESPACE, CLUSTER = "target-dev", "target-db"
RESTORE_NAMESPACE, RESTORE = "t35-restore-check", "target-db-restore"
BUCKET, PREFIX = "bravo-backup", "t35/target-db/"
ENDPOINT = "https://seaweedfs.target-dev.svc:8333"
WRITER_ID, READER_ID = "t35-backup-writer", "t35-backup-reader"
WRITER_STORE, READER_STORE = "target-db-backup", "target-db-backup-read"
CA_SECRET = "t35-backup-ca"
# SeaweedFS accepts only Pods with this label (target-workloads objects.py NetworkPolicy).
CLIENT = {"bravo-object-client": "true"}
DELETE = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
# Policy changes need the bucket Admin action, which only the SeaweedFS owner's identity has.
PROTECT = ["s3:DeleteBucket", "s3:PutBucketVersioning"]


def identities():
    """Actions to merge into the existing target-seaweedfs-auth s3.json. Keys are generated in memory by the owner.

    SeaweedFS treats deletion as Write, so deletion is taken away by the bucket policy for every identity.
    """
    return [{"name": WRITER_ID, "actions": [f"{a}:{BUCKET}" for a in ("Read", "Write", "List")]},
            {"name": READER_ID, "actions": [f"{a}:{BUCKET}" for a in ("Read", "List")]}]


def bucket_policy():
    """Nobody deletes, including the app, the Executor and the owner.

    SeaweedFS 4.52 rejects an owner exception (Principal "*" with ArnNotEquals) as an invalid policy, so the
    owner cleans up by taking this policy down, deleting and putting it back (only the owner may change policy).
    """
    bucket = Template("arn:aws:s3:::${bucket}").substitute(bucket=BUCKET)
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "NobodyDeletesBackups", "Effect": "Deny", "Principal": "*", "Action": DELETE,
         "Resource": [bucket + "/*"]},
        {"Sid": "NobodyUnprotectsBackups", "Effect": "Deny", "Principal": "*", "Action": PROTECT,
         "Resource": [bucket]},
    ]}


def stores():
    destination = f"s3://{BUCKET}/{PREFIX}"
    namespace = {"apiVersion": "v1", "kind": "Namespace",
                 "metadata": {"name": RESTORE_NAMESPACE, "labels": {"component": "target"}}}
    return [
        object_store(namespace=NAMESPACE, name=WRITER_STORE, destination=destination, backend="seaweedfs",
                     credential_secret=WRITER_ID, endpoint=ENDPOINT, ca_secret=CA_SECRET),
        namespace,
        object_store(namespace=RESTORE_NAMESPACE, name=READER_STORE, destination=destination, backend="seaweedfs",
                     credential_secret=READER_ID, endpoint=ENDPOINT, ca_secret=CA_SECRET),
    ]


def archive():
    """Plugin and store-client label in one merge patch; the instance restarts once."""
    spec = archive_patch(namespace=NAMESPACE, source_cluster=CLUSTER, writer_store=WRITER_STORE,
                         pod_labels=CLIENT)["spec"]
    return {"spec": {"plugins": spec["plugins"], "inheritedMetadata": spec["inheritedMetadata"]}}


def backup(run_id):
    """One name per T35 run, so a lost response is re-read by name instead of creating a second Backup."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", run_id):
        raise ValueError("T35 run ID required: lowercase letters, digits and hyphens, at most 32")
    request = backup_request(namespace=NAMESPACE, name=f"{CLUSTER}-t35-{run_id}", source_cluster=CLUSTER)
    request["metadata"]["annotations"] = {"bravo.t35/run-id": run_id}
    return [request]


def restore(backup_id, image, storage_class, storage_gib):
    return [restore_cluster(namespace=RESTORE_NAMESPACE, name=RESTORE, source_cluster=CLUSTER,
                            reader_store=READER_STORE, backup_id=backup_id, image_digest=image,
                            storage_class=storage_class, storage_gib=storage_gib, pod_labels=CLIENT)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("identities", "bucket-policy", "stores", "archive-patch"):
        sub.add_parser(name)
    b = sub.add_parser("backup")
    b.add_argument("--run-id", required=True, help="fixed for this T35 run; reuse it after a lost response")
    r = sub.add_parser("restore")
    r.add_argument("--backup-id", required=True)
    r.add_argument("--image", required=True, help="the target PostgreSQL image, tag and digest")
    r.add_argument("--storage-class", required=True)
    r.add_argument("--storage-gib", type=int, default=1)
    args = parser.parse_args(argv)
    if args.step == "identities":
        result = identities()
    elif args.step == "bucket-policy":
        result = bucket_policy()
    elif args.step == "archive-patch":
        result = archive()
    else:
        items = {"stores": stores, "backup": lambda: backup(args.run_id),
                 "restore": lambda: restore(args.backup_id, args.image, args.storage_class, args.storage_gib)}[args.step]()
        result = {"apiVersion": "v1", "kind": "List", "items": items}
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
