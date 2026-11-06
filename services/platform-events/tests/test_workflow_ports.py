"""Deployment ports: required sections, optional sections hold, mounts and pins are checked."""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from test_cutover import BINDINGS

from bravo_platform_events import workflow_ports
from bravo_platform_events.workflow_host_server import build

RUNTIME = "c" * 40


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "k1"}
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog-key").write_bytes(b"k" * 40)
    (tmp_path / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    reader = {"version": 2, "tenant_id": "tenant-demo", "catalog_root": str(tmp_path / "catalog"),
              "catalog_key_file": str(tmp_path / "catalog-key"),
              "job_identity": {"issuer": "https://kubernetes.default.svc.cluster.local",
                               "audience": "bravo-subscription-broker",
                               "subject": "system:serviceaccount:bravo-initial:initial-proposal",
                               "jwks_file": str(tmp_path / "jwks.json")}}
    (tmp_path / "reader.json").write_text(json.dumps(reader))
    (tmp_path / "selections").mkdir()
    (tmp_path / "state").mkdir()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    (tmp_path / "app.pem").write_bytes(pem)
    monkeypatch.setattr(workflow_ports, "_executor", lambda config: (None, BINDINGS))
    config = {"version": 1, "executor": {"url": "https://executor.invalid", "ca_file": "/x", "token_file": "/y",
                                          "registered_pair": {}},
              "propose": {"reader_config": str(tmp_path / "reader.json"), "principal": "operator-x",
                          "runtime_source_sha": RUNTIME, "selections_dir": str(tmp_path / "selections"),
                          "demo_chat_clone": str(tmp_path / "state" / "demo-chat")}}

    def write(value):
        path = tmp_path / "ports.json"
        path.write_text(json.dumps(value))
        return str(path)
    return config, write, tmp_path


def test_happy_path_ports_wire_propose_shift_and_cutover(backend, mounts):
    config, write, _ = mounts
    ports = workflow_ports.create_ports(backend.store, write(config))
    assert set(ports["readers"]) == {"AWAIT_APPROVAL", "CUTOVER"} and set(ports["preparers"]) == {"PROPOSE"}
    host = build(backend.store, lambda store: workflow_ports.create_ports(store, write(config)))
    assert host.active() == []  # no plan yet; the host is constructible from the deployment config


def test_github_app_section_builds_a_writer_with_exact_permissions(backend, mounts):
    config, write, tmp = mounts
    config["propose"]["github_app"] = {"app_id": 5643581, "key_file": str(tmp / "app.pem"),
                                       "workflow_sha": "3a1a9135" + "0" * 32, "workflow_id": 408240730,
                                       "state_file": str(tmp / "state" / "dispatches.json")}
    assert "PROPOSE" in workflow_ports.create_ports(backend.store, write(config))["preparers"]


@pytest.mark.parametrize("change", [
    lambda c: c.pop("executor"),
    lambda c: c.update(version=2),
    lambda c: c.update(extra=1),
    lambda c: c["propose"].update(runtime_source_sha="short"),
    lambda c: c["propose"].update(selections_dir="relative"),
    lambda c: c["propose"].update(github_app={"app_id": 1}),
    lambda c: c.update(application="not a factory"),
    lambda c: c.update(watch={"profile_file": "/x"}),
])
def test_invalid_or_partial_configuration_is_refused(backend, mounts, change):
    config, write, _ = mounts
    change(config)
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        workflow_ports.create_ports(backend.store, write(config))


def test_executor_requires_https_and_its_own_ca(tmp_path):
    with pytest.raises(ValueError):
        workflow_ports._executor({"url": "http://executor", "ca_file": "/x", "token_file": "/y",
                                  "registered_pair": {}})
    with pytest.raises(ValueError):
        workflow_ports._executor({"url": "https://executor", "ca_file": str(tmp_path / "missing"),
                                  "token_file": "/y", "registered_pair": {}})
