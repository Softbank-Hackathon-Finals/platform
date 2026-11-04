"""infra/envs/target-backup: SeaweedFS registration, CNPG manifests and the verification verdict."""

import importlib.util
import sys
from pathlib import Path
from string import Template
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[3] / "infra/envs/target-backup"


def load(name):
    spec = importlib.util.spec_from_file_location(f"target_backup_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(HERE), *sys.path]):
        spec.loader.exec_module(module)
    return module


render, verify = load("render"), load("verify")
WRITER = "arn:aws:iam::000000000000:user/t35-backup-writer"
READER = "arn:aws:iam::000000000000:user/t35-backup-reader"


def test_identities_are_bucket_scoped_and_the_reader_cannot_write():
    writer, reader = render.identities()
    assert writer == {"name": "t35-backup-writer", "actions": ["Read:bravo-backup", "Write:bravo-backup", "List:bravo-backup"]}
    assert reader == {"name": "t35-backup-reader", "actions": ["Read:bravo-backup", "List:bravo-backup"]}


def test_policy_only_denies_and_covers_deletion_and_protection():
    deletion, protect = render.bucket_policy()["Statement"]
    for statement in (deletion, protect):
        assert statement["Effect"] == "Deny" and statement["Principal"] == {"AWS": [WRITER, READER]}
    assert set(deletion["Action"]) == {"s3:DeleteObject", "s3:DeleteObjectVersion"}
    assert deletion["Resource"] == [Template("arn:aws:s3:::${b}/*").substitute(b="bravo-backup")]
    assert {"s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning", "s3:DeleteBucket"} == set(protect["Action"])


def test_stores_use_tls_and_separate_identities():
    writer, namespace, reader = render.stores()
    for store, secret, ns in ((writer, "t35-backup-writer", "target-dev"), (reader, "t35-backup-reader", "t35-restore-check")):
        config = store["spec"]["configuration"]
        assert store["metadata"]["namespace"] == ns
        assert config["endpointURL"] == "https://seaweedfs.target-dev.svc:8333"
        assert config["endpointCA"] == {"name": "t35-backup-ca", "key": "ca.crt"}
        assert config["s3Credentials"]["accessKeyId"]["name"] == secret
        assert config["destinationPath"] == "s3://bravo-backup/t35/target-db/"
    assert namespace["metadata"]["name"] == "t35-restore-check"


def test_cluster_pods_carry_the_store_client_label():
    patch_spec = render.archive()["spec"]
    assert patch_spec["inheritedMetadata"] == {"labels": {"bravo-object-client": "true"}}
    assert patch_spec["plugins"][0]["parameters"] == {"barmanObjectName": "target-db-backup"}
    (restored,) = render.restore("20261105T010000", "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:" + "a" * 64,
                                 "local-path", 1)
    assert restored["metadata"]["namespace"] == "t35-restore-check"
    assert restored["spec"]["inheritedMetadata"] == {"labels": {"bravo-object-client": "true"}}
    assert "plugins" not in restored["spec"]  # never archives into the source prefix
    assert render.backup("r1")[0]["metadata"]["name"] == "target-db-t35-r1"


def test_verify_compares_policy_regardless_of_list_or_string():
    policy = render.bucket_policy()
    returned = {"Version": "2012-10-17", "Statement": [dict(s, Resource=s["Resource"][0]) for s in policy["Statement"]]}
    assert verify.normalized(returned) == verify.normalized(policy)
    weakened = {"Version": "2012-10-17", "Statement": policy["Statement"][1:]}
    assert verify.normalized(weakened) != verify.normalized(policy)


def test_verify_status():
    good = dict(verify.EXPECTED, versioning_enabled=True, policy_matches=True, probe_intact=True, admin_removed_probe=True)
    assert verify.status(good) == "PASS"
    assert verify.status(dict(good, **{"writer.delete": "ok"})) == "FAIL"
    assert verify.status(dict(good, policy_matches=False)) == "FAIL"
    assert verify.status(dict(good, **{"reader.get": "error:EndpointConnectionError"})) == "UNKNOWN"
    assert verify.status(dict(good, admin_removed_probe=False)) == "UNKNOWN"
