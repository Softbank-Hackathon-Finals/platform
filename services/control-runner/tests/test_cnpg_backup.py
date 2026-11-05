import pytest

from bravo_controls.cnpg_backup import (
    archive_patch,
    backup_request,
    object_store,
    restore_cluster,
    s3_delete_guard,
)


def store(**kw):
    values = dict(
        namespace="backup-test",
        name="writer",
        destination="s3://example-backup/app-a/",
        backend="aws-s3",
    )
    return object_store(**(values | kw))


def restore(**kw):
    values = dict(
        namespace="backup-test",
        name="restore-one",
        source_cluster="source-one",
        reader_store="reader",
        backup_id="20261103T010000",
        image_digest="ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:" + "a" * 64,
        storage_class="standard",
    )
    return restore_cluster(**(values | kw))


def test_aws_workload_identity_and_no_automatic_deletion():
    document = store()
    assert document["spec"]["configuration"]["s3Credentials"] == {"inheritFromIAMRole": True}
    assert "retentionPolicy" not in document["spec"]
    assert "serverName" not in document["spec"]["configuration"]


def test_seaweed_requires_tls_and_secret_references_only():
    document = store(
        backend="seaweedfs",
        credential_secret="writer-credential",
        endpoint="https://store.example.test:8334",
        ca_secret="private-ca",
    )
    config = document["spec"]["configuration"]
    assert config["endpointCA"] == {"name": "private-ca", "key": "ca.crt"}
    assert config["s3Credentials"]["accessKeyId"]["name"] == "writer-credential"


@pytest.mark.parametrize(
    "change",
    [
        {"destination": "s3://example-backup/"},
        {"destination": "s3://example-backup/app/../"},
        {"destination": "s3://example-backup/app/?x=1"},
        {"destination": "s3://user:password@example-backup/app/"},
        {"namespace": "../outside"},
        {"name": "UPPER"},
        {"backend": "unknown"},
        {"credential_secret": "long-lived-key"},
        {"endpoint": "https://wrong.example.test"},
    ],
)
def test_unsafe_archive_scope_rejected(change):
    with pytest.raises(ValueError):
        store(**change)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://store.example.test",
        "https://user:password@store.example.test",
        "https://store.example.test/path",
        "https://store.example.test?token=secret",
    ],
)
def test_insecure_seaweed_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        store(backend="seaweedfs", credential_secret="cred", endpoint=endpoint, ca_secret="ca")


def test_restore_selects_exact_backup_new_cluster_and_cannot_archive():
    doc = restore()
    assert doc["spec"]["bootstrap"]["recovery"]["recoveryTarget"]["backupID"] == "20261103T010000"
    assert doc["spec"]["externalClusters"][0]["plugin"]["parameters"]["serverName"] == "source-one"
    assert "plugins" not in doc["spec"]
    assert doc["spec"]["enableSuperuserAccess"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"name": "source-one"},
        {"backup_id": "latest"},
        {"image_digest": "postgres:latest"},
        {"storage_gib": True},
        {"storage_gib": 101},
        {"storage_class": "../bad"},
    ],
)
def test_unqualified_restore_rejected(change):
    with pytest.raises(ValueError):
        restore(**change)


def test_backup_and_archive_fragment_exact_plugin():
    doc = backup_request(namespace="test", name="backup-one", source_cluster="source-one")
    assert doc["spec"]["method"] == "plugin"
    patch = archive_patch(namespace="test", source_cluster="source-one", writer_store="writer")
    assert patch["spec"]["plugins"][0]["isWALArchiver"] is True
    assert "serviceAccountTemplate" not in patch["spec"]


def test_irsa_role_annotates_cluster_service_account_only():
    writer = "arn:aws:iam::123456789012:role/source-backup-writer"
    reader = "arn:aws:iam::123456789012:role/source-backup-reader"
    patch = archive_patch(
        namespace="test", source_cluster="source-one", writer_store="writer", iam_role_arn=writer
    )
    annotations = patch["spec"]["serviceAccountTemplate"]["metadata"]["annotations"]
    assert annotations == {"eks.amazonaws.com/role-arn": writer}
    restored = restore(iam_role_arn=reader)["spec"]
    assert restored["serviceAccountTemplate"]["metadata"]["annotations"] == {
        "eks.amazonaws.com/role-arn": reader
    }
    assert "serviceAccountTemplate" not in restore()["spec"]
    for bad in (
        "arn:aws:iam::123456789012:user/backup",
        "arn:aws:iam::123456789012:role/*",
        "arn:aws:iam::1234567890123:role/backup",
        "arn:aws:iam::123456789012:role/backup\n",
        123,
    ):
        with pytest.raises(ValueError):
            archive_patch(
                namespace="test", source_cluster="source-one", writer_store="w", iam_role_arn=bad
            )
        with pytest.raises(ValueError):
            restore(iam_role_arn=bad)


def test_delete_guard_exact_principals_prefix_and_versions():
    roles = ("arn:aws:iam::123456789012:role/app", "arn:aws:iam::123456789012:role/executor")
    doc = s3_delete_guard(bucket="example-backup", prefix="app-a/", denied_role_arns=roles)
    first, second = doc["Statement"]
    assert first["Effect"] == "Deny"
    assert first["Resource"].split(":", 5) == ["arn", "aws", "s3", "", "", "example-backup/app-a/*"]
    assert "s3:DeleteObjectVersion" in first["Action"]
    assert second["Action"] == "s3:DeleteBucket"
    assert first["Condition"]["ArnEquals"]["aws:PrincipalArn"] == list(roles)
    with pytest.raises(ValueError):
        s3_delete_guard(bucket="example-backup", prefix="", denied_role_arns=roles)
    with pytest.raises(ValueError):
        s3_delete_guard(
            bucket="example-backup", prefix="app-a/", denied_role_arns=(roles[0], roles[0])
        )


def test_pod_labels_reach_instance_and_recovery_pods_only_when_valid():
    labels = {"bravo-object-client": "true"}
    patch = archive_patch(namespace="test", source_cluster="source-one", writer_store="w", pod_labels=labels)
    assert patch["spec"]["inheritedMetadata"] == {"labels": labels}
    assert restore(pod_labels=labels)["spec"]["inheritedMetadata"] == {"labels": labels}
    assert "inheritedMetadata" not in restore()["spec"]
    for bad in ({}, {"Bad Key": "x"}, {"k": "bad value"}, {"k": 1}, "bravo-object-client=true",
                {f"k{i}": "v" for i in range(9)}):
        with pytest.raises(ValueError):
            restore(pod_labels=bad)
        with pytest.raises(ValueError):
            archive_patch(namespace="test", source_cluster="source-one", writer_store="w", pod_labels=bad)
