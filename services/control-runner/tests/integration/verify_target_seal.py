"""Owned OpenBao 2.7.2 TLS/seal qualification. RKE2 remains synthetic here.

Uses a loopback-only published port, non-root container and disk-backed PebbleDB
in the disposable container. No team resources, real secrets or remote writes.
Root token/unseal shares live only in this process for fixture setup and teardown.
"""

import io
import json
import platform
import ssl
import subprocess
import tarfile
import tempfile
import time
from dataclasses import replace
from http.client import HTTPSConnection
from pathlib import Path
from uuid import uuid4

from bravo_controls.runner import Environment, RunContext
from bravo_controls.target_secret_encryption import (
    RKE2_VERSION,
    SPEC,
    OpenBaoSealReader,
    TargetEncryptionRegistration,
    TargetSecretEncryptionProbe,
)
from bravo_controls.tool_reports import ToolRun

IMAGE = "quay.io/openbao/openbao@sha256:dcf8c71424760a76368966b07227fff45ff49944fbbd53994afb6b83d205b08b"
VERSION = "2.7.2"


def command(*args, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("owned seal fixture command failed")
    return result.stdout


def archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as stream:
        for name, data in files.items():
            entry = tarfile.TarInfo("tmp/qualification/" + name)
            entry.uid, entry.gid, entry.mode, entry.size = 100, 1000, 0o400, len(data)
            stream.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def certificate(root):
    # Dedicated self-signed listener trust for this isolated test only.
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(root / "tls.key"), "-out", str(root / "tls.crt"),
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost")
    (root / "tls.key").chmod(0o600)
    return (root / "tls.crt").read_text()


def main():
    if platform.machine() not in {"arm64", "aarch64"}:
        raise RuntimeError("this pinned qualification image requires native ARM64")
    meta = json.loads(command("docker", "image", "inspect", IMAGE))[0]
    if meta["Architecture"] != "arm64":
        raise RuntimeError("native image required")
    name = "bravo-t14-seal-" + uuid4().hex[:12]
    created = False
    results = {}
    with tempfile.TemporaryDirectory(prefix="t14-seal-") as folder:
        root = Path(folder)
        ca = certificate(root)
        try:
            command("docker", "create", "--name", name, "--label", "bravo.t14.fixture=" + name,
                    "--user", "100:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
                    "-p", "127.0.0.1::8200", "--entrypoint", "bao", IMAGE,
                    "server", "-config=/tmp/qualification/bao.hcl")
            created = True
            config = b'''ui = false
disable_mlock = true
api_addr = "https://localhost:8200"
listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "127.0.0.1:8201"
  tls_cert_file = "/tmp/qualification/tls.crt"
  tls_key_file = "/tmp/qualification/tls.key"
  tls_min_version = "tls12"
}
storage "pebbledb" { path = "/tmp/owned-baodata" }
'''
            command("docker", "cp", "-", name + ":/", input=archive({
                "bao.hcl": config, "tls.crt": ca.encode(), "tls.key": (root / "tls.key").read_bytes(),
            }))
            # Tar's implicit parent directory is root-owned; only this fixture
            # gets a writable data directory from the running non-root process.
            command("docker", "start", name)
            info = json.loads(command("docker", "inspect", name))[0]
            binding = info["NetworkSettings"]["Ports"]["8200/tcp"]
            if len(binding) != 1 or binding[0]["HostIp"] != "127.0.0.1":
                raise RuntimeError("loopback-only fixture required")
            port = int(binding[0]["HostPort"])
            reader = OpenBaoSealReader(f"https://localhost:{port}", ca)

            def api(method, path, body=None, token=None):
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                tls.load_verify_locations(cadata=ca)
                connection = HTTPSConnection("localhost", port, timeout=5, context=tls)
                try:
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["X-Vault-Token"] = token
                    connection.request(method, "/v1/" + path,
                                       json.dumps(body) if body is not None else None, headers)
                    response = connection.getresponse()
                    raw = response.read(16385)
                    if response.status not in {200, 204} or len(raw) > 16384:
                        raise RuntimeError("owned fixture API failed")
                    return json.loads(raw) if raw else {}
                finally:
                    connection.close()

            for attempt in range(30):
                try:
                    uninitialized = reader()
                    break
                except ValueError:
                    if attempt == 29:
                        raise RuntimeError("owned OpenBao did not start") from None
                    time.sleep(0.2)
            results["uninitialized_observed"] = uninitialized["initialized"] is False
            initialized = api("PUT", "sys/init", {"secret_shares": 1, "secret_threshold": 1})
            share, token = initialized["keys_base64"][0], initialized["root_token"]
            api("PUT", "sys/unseal", {"key": share})
            status = reader()
            if status["version"] != VERSION or status["storage_type"] != "pebbledb":
                raise RuntimeError("qualified OpenBao version/storage mismatch")
            environment = Environment("fixture-target", "target", "local:seal-qualification")
            target = TargetEncryptionRegistration(
                "fixture-chat", environment, "synthetic-rke2", ("synthetic-node",),
                status["cluster_id"], VERSION, "pebbledb", "owned-container", "owned-disk",
                "synthetic-app-secret", "synthetic-delivery",
            )
            ctx = RunContext("seal-" + uuid4().hex, target.app, environment)

            def node(c, t, uid):
                return ToolRun("rke2-secrets-encrypt", RKE2_VERSION, c.run_id, uid,
                               json.dumps({"enable": True, "stage": "start", "hashmatch": True,
                                           "activekey": "AES-CBC fixture"}).encode(), 0, True)

            def observe(selected=reader, registration=target):
                evidence = []
                obs = TargetSecretEncryptionProbe(
                    registration, read_registration=lambda *_: registration.binding(),
                    read_node_status=node, read_bao_status=selected, evidence_sink=evidence.append,
                )(ctx, SPEC)
                assert token not in repr(evidence) and share not in repr(evidence)
                return {"observed": obs.observed, "reason": obs.unknown_reason}

            results["unsealed_with_synthetic_rke2"] = observe()
            assert results["unsealed_with_synthetic_rke2"]["observed"] == "enabled"
            results["wrong_cluster"] = observe(registration=replace(target, bao_cluster_id=str(uuid4())))
            assert results["wrong_cluster"]["observed"] is None
            results["wrong_hostname"] = observe(OpenBaoSealReader(f"https://127.0.0.1:{port}", ca))
            assert results["wrong_hostname"]["observed"] is None
            other = root / "other"
            other.mkdir()
            results["wrong_ca"] = observe(OpenBaoSealReader(f"https://localhost:{port}", certificate(other)))
            assert results["wrong_ca"]["observed"] is None
            api("PUT", "sys/seal", token=token)
            results["sealed"] = observe()
            assert results["sealed"]["observed"] is None
            api("PUT", "sys/unseal", {"key": share})
            results["unsealed_again"] = observe()
            assert results["unsealed_again"]["observed"] == "enabled"
            command("docker", "restart", name)
            # Docker may reassign an ephemeral published port on restart.
            info = json.loads(command("docker", "inspect", name))[0]
            binding = info["NetworkSettings"]["Ports"]["8200/tcp"]
            if len(binding) != 1 or binding[0]["HostIp"] != "127.0.0.1":
                raise RuntimeError("restarted listener not confined to loopback")
            port = int(binding[0]["HostPort"])
            reader = OpenBaoSealReader(f"https://localhost:{port}", ca)
            for attempt in range(50):
                try:
                    restarted = reader()
                    break
                except ValueError:
                    if attempt == 49:
                        raise
                    time.sleep(0.2)
            results["restart_preserved_initialized_and_sealed"] = (
                restarted["initialized"] is True and restarted["sealed"] is True)
            assert results["restart_preserved_initialized_and_sealed"]
            api("PUT", "sys/unseal", {"key": share})
            results["restart_preserved_cluster"] = reader()["cluster_id"] == target.bao_cluster_id
            assert results["restart_preserved_cluster"]
        finally:
            if created:
                info = json.loads(command("docker", "inspect", name))[0]
                if info["Config"]["Labels"].get("bravo.t14.fixture") != name:
                    raise RuntimeError("fixture ownership changed")
                command("docker", "rm", "-f", name)
                assert not command("docker", "ps", "-aq", "--filter", "label=bravo.t14.fixture=" + name).strip()
    print(json.dumps({"openbao_version": VERSION, "image": IMAGE, "results": results,
                      "rke2_actual": False, "owned_container_removed": True}, sort_keys=True))


if __name__ == "__main__":
    main()
