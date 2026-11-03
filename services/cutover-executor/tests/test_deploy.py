import pytest

from bravo_cutover.deploy import manifests

IMAGE = "registry.example.invalid/team/executor@sha256:" + "a" * 64


def test_runtime_has_no_migrator_credential_or_local_cluster_token():
    sa, deployment, service = manifests(IMAGE)["items"]
    pod = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert sa["automountServiceAccountToken"] is False
    assert pod["automountServiceAccountToken"] is False
    assert "migration" not in str(pod)
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert service["spec"]["type"] == "ClusterIP"


def test_migration_has_no_remote_cluster_credentials_or_http_server():
    _, job = manifests(IMAGE, migration=True)["items"]
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["command"] == ["python", "-m", "bravo_cutover.migrate"]
    assert "cutover-executor-runtime" not in str(pod)
    assert "ports" not in pod["containers"][0]
    assert job["spec"]["backoffLimit"] == 0


def test_mutable_image_tag_is_rejected():
    with pytest.raises(ValueError, match="digest"):
        manifests("registry.example.invalid/team/executor:latest")
