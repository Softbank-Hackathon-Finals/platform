"""Deployment boundaries are checked before any Kubernetes mutation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

RENDER = Path(__file__).resolve().parents[1] / "deploy/registration/render.py"


def render(tmp_path, change=None):
    inputs = {
        "image": "registry.example/registration@sha256:" + "a" * 64,
        "source_revision": "b" * 40,
        "collector_secret": "registration-collectors",
        "tls_secret": "registration-tls",
        "egress_ipv4": {
            "github.com": ["192.0.2.1"],
            "sts.ap-northeast-2.amazonaws.com": ["192.0.2.2"],
            "SOURCE.example": ["192.0.2.3"],
        },
    }
    if change:
        change(inputs)
    catalog = {
        "sso_verifier": {"region": "ap-northeast-2"},
        "environments": [{"endpoint": "https://SOURCE.example"}],
        "releases": [{"repo_url": "https://github.com/example/app", "revision": "c" * 40}],
    }
    for name, value in {"inputs": inputs, "catalog": catalog, "grants": []}.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    output = tmp_path / "rendered.json"
    args = [sys.executable, str(RENDER)]
    for name in ("inputs", "catalog", "grants"):
        args.extend(["--" + name, str(tmp_path / (name + ".json"))])
    result = subprocess.run(
        args + ["--output", str(output)], capture_output=True, text=True, check=False
    )
    return result, output


def test_collector_and_checkout_cannot_receive_runtime_admin_authority(tmp_path):
    result, output = render(tmp_path)
    assert result.returncode == 0, result.stderr
    objects = json.loads(output.read_text())["items"]
    deployment = next(x for x in objects if x["kind"] == "Deployment")
    spec = deployment["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert all(h == h.lower() for entry in spec["hostAliases"] for h in entry["hostnames"])
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "checkout", "mountPath": "/checkout"}
    ]
    assert all(m["readOnly"] for m in spec["containers"][0]["volumeMounts"])
    assert not any(x["kind"] in {"Secret", "RoleBinding", "ClusterRoleBinding"} for x in objects)
    db = next(x for x in spec["volumes"] if x["name"] == "ca")["secret"]
    assert db["items"] == [{"key": "ca.crt", "path": "ca.crt"}]
    assert spec["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(image="registry.example/registration:latest"),
        lambda x: x["egress_ipv4"].update({"unregistered.example": ["192.0.2.4"]}),
        lambda x: x["egress_ipv4"].update({"github.com": ["0.0.0.0/0"]}),
        lambda x: x["egress_ipv4"].update({"github.com": ["127.0.0.1"]}),
    ],
)
def test_unbound_image_or_egress_does_not_produce_a_manifest(tmp_path, change):
    result, output = render(tmp_path, change)
    assert result.returncode != 0
    assert not output.exists()
