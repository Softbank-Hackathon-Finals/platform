#!/usr/bin/env python3
"""Render T35 source backup manifests from this stack's registration output.

Run from the repository root with `uv run --project services/control-runner`. Prints a
kubectl List, or for role-patch and archive-patch a merge patch for the existing source Cluster.
Rendering is not apply: the source owner reviews and applies it.
"""

import argparse
import json
import sys
from datetime import UTC, datetime

from bravo_controls.cnpg_backup import archive_patch, backup_request, object_store, restore_cluster

WRITER_STORE = "source-db-backup"
READER_STORE = "source-db-backup-read"


def stores(reg):
    """Writer store beside the source DB, read-only store in the separate restore namespace."""
    writer, reader = reg["writer"], reg["reader"]
    namespace = {"apiVersion": "v1", "kind": "Namespace",
                 "metadata": {"name": reader["namespace"], "labels": {"component": "source"}}}
    return [
        object_store(namespace=writer["namespace"], name=WRITER_STORE,
                     destination=reg["destination"], backend="aws-s3"),
        namespace,
        object_store(namespace=reader["namespace"], name=READER_STORE,
                     destination=reg["destination"], backend="aws-s3"),
    ]


def cluster_patches(reg):
    """Only the fields T35 adds to the source Cluster; the source has no other plugin.

    Two steps: the role annotation must be on the ServiceAccount before the plugin restarts
    the instance, because EKS injects IRSA credentials only when a Pod is created.
    """
    writer = reg["writer"]
    spec = archive_patch(namespace=writer["namespace"], source_cluster=writer["name"],
                         writer_store=WRITER_STORE, iam_role_arn=reg["writer_role_arn"])["spec"]
    return {"role-patch": {"spec": {"serviceAccountTemplate": spec["serviceAccountTemplate"]}},
            "archive-patch": {"spec": {"plugins": spec["plugins"]}}}


def backup(reg, now):
    writer = reg["writer"]
    name = f"{writer['name']}-t35-{now:%Y%m%dt%H%M%S}"
    return [backup_request(namespace=writer["namespace"], name=name, source_cluster=writer["name"])]


def restore(reg, backup_id, image, storage_class, storage_gib):
    # The restore Cluster's name is its ServiceAccount's name, which the reader role trusts.
    reader = reg["reader"]
    return [restore_cluster(namespace=reader["namespace"], name=reader["name"],
                            source_cluster=reg["writer"]["name"], reader_store=READER_STORE,
                            backup_id=backup_id, image_digest=image, storage_class=storage_class,
                            storage_gib=storage_gib, iam_role_arn=reg["reader_role_arn"])]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True, type=argparse.FileType(),
                        help="terraform output -json backup_registration")
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("stores")
    sub.add_parser("role-patch")
    sub.add_parser("archive-patch")
    sub.add_parser("backup")
    r = sub.add_parser("restore")
    r.add_argument("--backup-id", required=True)
    r.add_argument("--image", required=True, help="the source PostgreSQL image, tag and digest")
    r.add_argument("--storage-class", required=True)
    r.add_argument("--storage-gib", type=int, default=1)
    args = parser.parse_args(argv)
    reg = json.load(args.registration)
    if args.step in ("role-patch", "archive-patch"):
        json.dump(cluster_patches(reg)[args.step], sys.stdout, indent=2)
        print()
        return
    if args.step == "stores":
        items = stores(reg)
    elif args.step == "backup":
        items = backup(reg, datetime.now(UTC))
    else:
        items = restore(reg, args.backup_id, args.image, args.storage_class, args.storage_gib)
    json.dump({"apiVersion": "v1", "kind": "List", "items": items}, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
