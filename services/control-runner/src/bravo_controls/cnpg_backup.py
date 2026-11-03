"""T35 bounded CNPG/Barman manifests for environment-owner review.

Rendering is not authorization or apply. Registered endpoints, service accounts,
PVC isolation and server-side UID preconditions are checked by the effect executor.
Never attach app credentials or blanket deletion privileges to these resources.
"""

import re
from datetime import datetime
from urllib.parse import urlsplit

PLUGIN = "barman-cloud.cloudnative-pg.io"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
IMAGE = re.compile(
    r"ghcr\.io/cloudnative-pg/postgresql(?:-standard)?:[0-9]{2}\.[0-9]+(?:-[a-z0-9.-]+)?@sha256:[0-9a-f]{64}\Z"
)


def _names(*values):
    if any(not isinstance(v, str) or not NAME.fullmatch(v) for v in values):
        raise ValueError("DNS label required")


def _object(api, kind, namespace, name, spec):
    _names(namespace, name)
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {
            "namespace": namespace,
            "name": name,
            "labels": {"app.kubernetes.io/part-of": "bravo", "component": "backup"},
        },
        "spec": spec,
    }


def object_store(
    *, namespace, name, destination, backend, credential_secret=None, endpoint=None, ca_secret=None
):
    """Separate writer/read-only ObjectStores should use different identities.

    No retentionPolicy: automatic deletion must not be enabled by the backup
    writer. A separately approved lifecycle custodian owns expiry/retention.
    """
    if not isinstance(destination, str):
        raise ValueError("registered archive path required")
    parsed = urlsplit(destination)
    if (
        parsed.scheme != "s3"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parsed.hostname)
        or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/", parsed.path)
    ):
        raise ValueError("explicit bucket and non-root archive prefix required")
    config = {
        "destinationPath": destination,
        "wal": {"compression": "gzip"},
        "data": {"compression": "gzip", "jobs": 1},
    }
    if backend == "aws-s3":
        if any(v is not None for v in (credential_secret, endpoint, ca_secret)):
            raise ValueError("AWS path uses registered workload IAM, not supplied keys/endpoints")
        config["s3Credentials"] = {"inheritFromIAMRole": True}
    elif backend == "seaweedfs":
        _names(credential_secret, ca_secret)
        if not isinstance(endpoint, str):
            raise ValueError("TLS endpoint required")
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("registered HTTPS endpoint required")
        # Registration separately verifies DNS, ownership and egress allowlists.
        config.update(
            {
                "endpointURL": endpoint,
                "endpointCA": {"name": ca_secret, "key": "ca.crt"},
                "s3Credentials": {
                    "accessKeyId": {"name": credential_secret, "key": "ACCESS_KEY_ID"},
                    "secretAccessKey": {"name": credential_secret, "key": "ACCESS_SECRET_KEY"},
                },
            }
        )
    else:
        raise ValueError("unqualified backup backend")
    return _object(
        "barmancloud.cnpg.io/v1",
        "ObjectStore",
        namespace,
        name,
        {
            "configuration": config,
            "instanceSidecarConfiguration": {
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "1", "memory": "256Mi"},
                }
            },
        },
    )


def backup_request(*, namespace, name, source_cluster):
    _names(source_cluster)
    return _object(
        "postgresql.cnpg.io/v1",
        "Backup",
        namespace,
        name,
        {
            "cluster": {"name": source_cluster},
            "method": "plugin",
            "pluginConfiguration": {"name": PLUGIN},
        },
    )


def archive_patch(*, namespace, source_cluster, writer_store):
    """Reviewable fragment; owner must preserve other plugins when applying."""
    _names(writer_store)
    return _object(
        "postgresql.cnpg.io/v1",
        "Cluster",
        namespace,
        source_cluster,
        {
            "plugins": [
                {
                    "name": PLUGIN,
                    "isWALArchiver": True,
                    "parameters": {"barmanObjectName": writer_store},
                }
            ]
        },
    )


def restore_cluster(
    *,
    namespace,
    name,
    source_cluster,
    reader_store,
    backup_id,
    image_digest,
    storage_class,
    storage_gib=1,
):
    _names(source_cluster, reader_store, storage_class)
    if name == source_cluster:
        raise ValueError("restore must use a new cluster")
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}", backup_id):
        raise ValueError("completed Barman backup ID required, never latest")
    datetime.strptime(backup_id, "%Y%m%dT%H%M%S")
    if not isinstance(image_digest, str) or not IMAGE.fullmatch(image_digest):
        raise ValueError("qualified PostgreSQL image digest required")
    if type(storage_gib) is not int or not 1 <= storage_gib <= 100:
        raise ValueError("approved bounded restore storage required")
    return _object(
        "postgresql.cnpg.io/v1",
        "Cluster",
        namespace,
        name,
        {
            "instances": 1,
            "imageName": image_digest,
            "enableSuperuserAccess": False,
            "bootstrap": {
                "recovery": {
                    "source": "source",
                    "recoveryTarget": {"backupID": backup_id, "targetImmediate": True},
                }
            },
            "externalClusters": [
                {
                    "name": "source",
                    "plugin": {
                        "name": PLUGIN,
                        "parameters": {
                            "barmanObjectName": reader_store,
                            "serverName": source_cluster,
                        },
                    },
                }
            ],
            # No WAL archiver: a restore verification must not write into the source archive.
            "storage": {"size": f"{storage_gib}Gi", "storageClass": storage_class},
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
        },
    )


def s3_delete_guard(*, bucket, prefix, denied_role_arns):
    """Merge-reviewed bucket policy statements, not replacement for existing policy.

    This denies deletion by the registered app/executor roles even if another
    identity policy allows it. It is not Object Lock or administrator protection.
    """
    if (
        not isinstance(bucket, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or not isinstance(prefix, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/", prefix)
        or not isinstance(denied_role_arns, tuple)
        or not 2 <= len(denied_role_arns) <= 10
        or len(set(denied_role_arns)) != len(denied_role_arns)
        or any(
            not isinstance(arn, str)
            or not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}", arn)
            for arn in denied_role_arns
        )
    ):
        raise ValueError("exact archive scope and distinct app/executor IAM roles required")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyBackupObjectDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket}/{prefix}*",
                "Condition": {"ArnEquals": {"aws:PrincipalArn": list(denied_role_arns)}},
            },
            {
                "Sid": "DenyBackupBucketDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:DeleteBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"ArnEquals": {"aws:PrincipalArn": list(denied_role_arns)}},
            },
        ],
    }
