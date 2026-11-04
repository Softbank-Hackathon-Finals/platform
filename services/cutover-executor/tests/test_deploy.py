from types import SimpleNamespace

import pytest

from bravo_cutover.deploy import manifests

IMAGE = "registry.example.invalid/team/executor@sha256:" + "a" * 64


@pytest.mark.parametrize("contents,expected", [
    (None, FileNotFoundError),
    ("", ValueError),
    ("r" * 32, ValueError),
])
def test_invalid_mounted_command_credential_is_rejected_before_recovery(tmp_path, monkeypatch, contents, expected):
    from bravo_cutover import server

    reader, command = tmp_path / "read", tmp_path / "command"
    reader.write_text("r" * 32)
    if contents is not None:
        command.write_text(contents)
    monkeypatch.setenv("EXECUTOR_CONFIG", "test-only")
    monkeypatch.setenv("EXECUTOR_API_TOKEN_FILE", str(reader))
    monkeypatch.setenv("EXECUTOR_COMMAND_TOKEN_FILE", str(command))
    monkeypatch.setenv("EXECUTOR_DB", "unused")
    monkeypatch.setattr(server, "load_config", lambda _: SimpleNamespace(deployment_registry=lambda: object()))
    monkeypatch.setattr(server, "Store", lambda _: pytest.fail("opened state DB before checking credentials"))
    with pytest.raises(expected):
        server.create_app()



def test_runtime_has_no_migrator_credential_or_local_cluster_token():
    sa, deployment, service = manifests(IMAGE)["items"]
    pod = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert sa["automountServiceAccountToken"] is False
    assert pod["automountServiceAccountToken"] is False
    command = next(volume for volume in pod["volumes"] if volume["name"] == "command")
    assert command["secret"]["secretName"] == "cutover-executor-command"
    assert command["secret"]["items"] == [{"key": "token", "path": "token"}]
    assert "migration" not in str(pod)
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert service["spec"]["type"] == "ClusterIP"


def test_migration_has_no_remote_cluster_credentials_or_http_server():
    _, job = manifests(IMAGE, migration=True)["items"]
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["command"] == ["python", "-m", "bravo_cutover.migrate"]
    assert "cutover-executor-runtime" not in str(pod)
    assert "cutover-executor-command" not in str(pod)
    assert "ports" not in pod["containers"][0]
    assert job["spec"]["backoffLimit"] == 0


def test_mutable_image_tag_is_rejected():
    with pytest.raises(ValueError, match="digest"):
        manifests("registry.example.invalid/team/executor:latest")


@pytest.mark.parametrize("migration", [False, True])
def test_both_database_clients_mount_dedicated_state_ca(migration):
    workload = manifests(IMAGE, migration=migration)["items"][1]
    pod = workload["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "state-ca")
    assert volume["secret"]["secretName"] == "cutover-executor-state-ca"
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "state-ca")
    assert mount == {"name": "state-ca", "mountPath": "/run/state-ca", "readOnly": True}
