"""The workflow host factory fails closed: no usable target kubeconfig, kubectl or receipt mount is a
named hold, and nothing can fall back to the Pod's own (in-cluster) Kubernetes identity."""

import json
import os
import stat
import subprocess

import pytest

from bravo_apply import workflow_factory as wf
from bravo_apply.deploy import KubectlKube


def kubeconfig(**overrides):
    value = {"apiVersion": "v1", "kind": "Config", "current-context": "apply",
             "clusters": [{"name": "target", "cluster": {"server": "https://target.example.invalid:443",
                                                         "certificate-authority-data": "Q0E="}}],
             "users": [{"name": "deployer", "user": {"token": "fixture-not-a-token"}}],
             "contexts": [{"name": "apply", "context": {"cluster": "target", "user": "deployer",
                                                        "namespace": "demo-chat"}}]}
    for key, change in overrides.items():
        change(value) if callable(change) else value.update({key: change})
    return value


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text("#!/bin/sh\nexit 0\n")
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    secret = tmp_path / "secret"
    secret.mkdir()
    # Secret volumes mount each key through ..data symlinks; that layout must be accepted.
    data = secret / "..2026_11_07"
    data.mkdir()
    (data / "kubeconfig").write_text(json.dumps(kubeconfig()))
    (secret / "kubeconfig").symlink_to(data / "kubeconfig")
    pvc = tmp_path / "t31"
    pvc.mkdir()
    config = {"version": 1, "kubeconfig_file": str(secret / "kubeconfig"),
              "receipts_dir": str(pvc / "applications"),
              "target": {"environment_id": "env-onprem", "namespace": "demo-chat", "deployment": "demo-chat",
                         "container": "app", "repository": "ghcr.io/softbank-hackathon-finals/demo-chat"},
              "host_subject": "workflow-host", "trusted_ports": "deploy.ports:trusted"}
    path = tmp_path / "apply.json"

    def write(value=None):
        path.write_text(json.dumps(value or config))
        return wf.load_config(path)
    return config, write, tmp_path


def test_mounted_kubeconfig_kubectl_and_receipts_build_the_real_adapter(mounted):
    config, write, tmp = mounted
    loaded = write()
    kube = wf.target_kube(loaded)
    assert isinstance(kube, KubectlKube) and kube.cluster == "kubectl"
    assert kube.kubeconfig == config["kubeconfig_file"]
    root = wf.receipts(loaded)
    assert root.is_dir() and stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize("reason,change", [
    ("missing", lambda p: p.unlink()),
    ("empty", lambda p: p.write_text("")),
    ("empty", lambda p: p.write_text("\n  \n")),
    ("no_current_context", lambda p: p.write_text("{}")),
    ("unparseable", lambda p: p.write_text("{not json: [")),
    ("no_current_context", lambda p: p.write_text(json.dumps(kubeconfig(**{"current-context": ""})))),
    ("context_unresolved", lambda p: p.write_text(json.dumps(kubeconfig(**{"current-context": "other"})))),
    ("context_unresolved", lambda p: p.write_text(json.dumps(kubeconfig(users=[])))),
    ("cluster_not_verified_https", lambda p: p.write_text(json.dumps(kubeconfig(
        clusters=[{"name": "target", "cluster": {"server": "http://target.example.invalid"}}])))),
    ("cluster_not_verified_https", lambda p: p.write_text(json.dumps(kubeconfig(
        clusters=[{"name": "target", "cluster": {"server": "https://t.example.invalid",
                                                 "insecure-skip-tls-verify": True}}])))),
    ("user_credential_missing", lambda p: p.write_text(json.dumps(kubeconfig(
        users=[{"name": "deployer", "user": {}}])))),
    ("exec_command_missing", lambda p: p.write_text(json.dumps(kubeconfig(
        users=[{"name": "deployer", "user": {"exec": {"command": "/nonexistent/credentials.py"}}}])))),
    ("namespace_mismatch", lambda p: p.write_text(json.dumps(kubeconfig(contexts=[
        {"name": "apply", "context": {"cluster": "target", "user": "deployer", "namespace": "kube-system"}}])))),
    ("too_large", lambda p: p.write_text(" " * (wf.MAX_KUBECONFIG + 1))),
])
def test_unusable_kubeconfig_is_a_named_hold_without_an_adapter(mounted, reason, change):
    config, write, _ = mounted
    loaded = write()
    change(loaded.kubeconfig_file.resolve())
    with pytest.raises(wf.TargetUnavailable) as error:
        wf.target_kube(loaded)
    assert (error.value.code, error.value.detail) == ("t31_target_kubeconfig_unavailable", reason)
    assert "fixture-not-a-token" not in str(error.value)  # never the file content


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_kubeconfig_is_refused(mounted):
    _, write, _ = mounted
    loaded = write()
    loaded.kubeconfig_file.resolve().chmod(0)
    with pytest.raises(wf.TargetUnavailable) as error:
        wf.target_kube(loaded)
    assert error.value.detail == "unreadable"


def test_missing_kubectl_and_receipt_mount_are_named(mounted, monkeypatch):
    config, write, tmp = mounted
    loaded = write()
    monkeypatch.setenv("PATH", str(tmp / "empty"))
    with pytest.raises(wf.TargetUnavailable, match="t31_kubectl_unavailable"):
        wf.target_kube(loaded)
    config["receipts_dir"] = str(tmp / "no-pvc" / "applications")
    with pytest.raises(wf.TargetUnavailable, match="t31_receipts_mount_unavailable"):
        wf.receipts(write(config))
    assert not (tmp / "no-pvc").exists()  # the mount itself is never created


@pytest.mark.parametrize("change", [
    lambda c: c.update(version=2),
    lambda c: c.update(extra=1),
    lambda c: c.pop("trusted_ports"),
    lambda c: c.update(kubeconfig_file="relative/kubeconfig"),
    lambda c: c.update(receipts_dir="/var/lib/bravo/../etc"),
    lambda c: c["target"].update(namespace="Demo_Chat"),
    lambda c: c["target"].update(repository="ghcr.io/x/demo-chat:latest"),
    lambda c: c["target"].update(repository="ghcr.io/x/demo-chat@sha256:" + "a" * 64),
    lambda c: c["target"].pop("container"),
    lambda c: c.update(trusted_ports="not a factory"),
    lambda c: c.update(host_subject=""),
])
def test_malformed_apply_configuration_fails_startup(mounted, change):
    config, write, _ = mounted
    change(config)
    with pytest.raises(ValueError, match="t31_apply_"):
        write(config)


def test_kubectl_never_sees_in_cluster_service_variables(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setenv("KUBECONFIG", "/somewhere/else")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(
            {"spec": {"template": {"spec": {"containers": [{"name": "app", "image": "x@sha256:" + "a" * 64}]}}}}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    KubectlKube("/mounted/kubeconfig").current_image("demo-chat", "demo-chat", "app")
    assert seen["cmd"][:3] == ["kubectl", "--kubeconfig", "/mounted/kubeconfig"]
    assert not {"KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBECONFIG"} & set(seen["env"])
    assert "PATH" in seen["env"]
