"""Opt-in local Docker qualification; no cloud credentials or published ports.

Run from the package with PYTHONPATH=src. Records contain synthetic canaries.
The receiver is a test file sink, not a production audit storage implementation.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from bravo_controls.audit_forwarding import AuditForwardingRegistration, collector_config

IMAGE = "otel/opentelemetry-collector-contrib@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b"


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=45, **kwargs).stdout


def certificates(root):
    conf = root / "openssl.cnf"
    conf.write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=audit-receiver\n[ext]\nsubjectAltName=DNS:audit-receiver\nextendedKeyUsage=serverAuth,clientAuth\n")
    command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Audit test CA", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.crt"))
    command("openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", str(conf), "-keyout", str(root / "tls.key"), "-out", str(root / "tls.csr"))
    command("openssl", "x509", "-req", "-in", str(root / "tls.csr"), "-CA", str(root / "ca.crt"), "-CAkey", str(root / "ca.key"), "-CAcreateserial", "-days", "1", "-extfile", str(conf), "-extensions", "ext", "-out", str(root / "tls.crt"))
    for p in root.glob("*.key"):
        p.chmod(0o600)


def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            batch = json.loads(line)
        except json.JSONDecodeError:
            continue  # the writer may still be flushing; final assertions check all markers
        for resource in batch.get("resourceLogs", []):
            attrs = {a["key"]: a["value"].get("stringValue") for a in resource.get("resource", {}).get("attributes", [])}
            for scope in resource.get("scopeLogs", []):
                result += [(attrs, row) for row in scope.get("logRecords", [])]
    return result


def wait_for(check, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.5)
    raise RuntimeError("bounded collector assertion timed out")


def run(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    for name in ("tls", "checkpoints", "output", "input", "audit"):
        (root / name).mkdir()
    certificates(root / "tls")
    uid = "11111111-1111-4111-8111-111111111111"
    poddir = f"audit-test_audit-db-1_{uid}"
    logs = root / "input" / poddir / "postgres"
    logs.mkdir(parents=True)
    source = logs / "0.log"
    source.touch()
    audit = root / "audit" / "audit.log"
    audit.touch()
    bind = AuditForwardingRegistration("source", "source", "sha256:" + "a" * 64,
                                      "audit-test", "audit-db-1", uid,
                                      "22222222-2222-4222-8222-222222222222",
                                      "https://audit-receiver:8443", True)
    config = collector_config(bind)
    # Synthetic-only local fixture: retain retry diagnostics to distinguish TLS rejection.
    config["service"]["telemetry"]["logs"]["level"] = "info"
    (root / "sender.json").write_text(json.dumps(config))
    receiver = {
        "receivers": {"otlp": {"protocols": {"http": {
            "endpoint": "0.0.0.0:8443", "tls": {
                "cert_file": "/tls/tls.crt", "key_file": "/tls/tls.key", "client_ca_file": "/tls/ca.crt", "min_version": "1.2"}}}}},
        "exporters": {"file": {"path": "/output/logs.json", "append": True, "flush_interval": "100ms"}},
        "service": {"pipelines": {"logs": {"receivers": ["otlp"], "exporters": ["file"]}},
                    "telemetry": {"logs": {"level": "error"}}},
    }
    (root / "receiver.json").write_text(json.dumps(receiver))
    suffix = uuid4().hex[:12]
    net, send, recv = ("bravo-audit-" + suffix + x for x in ("-net", "-send", "-recv"))
    created = []
    result = {"image": IMAGE, "synthetic_input": True, "cloud_resources_changed": False}
    try:
        command("docker", "network", "create", "--internal", net)
        created.append(("network", net))
        common = ["docker", "run", "-d", "--network", net, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "256m", "--cpus", "1"]
        command(*common, "--name", recv, "--network-alias", "audit-receiver",
                "-v", f"{root}/receiver.json:/etc/config.json:ro", "-v", f"{root}/tls:/tls:ro", "-v", f"{root}/output:/output", IMAGE, "--config=/etc/config.json")
        created.append(("container", recv))
        command(*common, "--name", send, "-v", f"{root}/sender.json:/etc/config.json:ro",
                "-v", f"{root}/tls:/var/run/bravo-audit/tls:ro", "-v", f"{root}/checkpoints:/var/lib/bravo-audit/checkpoints",
                "-v", f"{root}/input:/var/log/pods:ro", "-v", f"{root}/audit:/var/log/bravo-audit/kubernetes:ro",
                IMAGE, "--config=/etc/config.json")
        created.append(("container", send))

        def emit(number):
            marker = "bravo_audit_" + f"{number:032x}"
            body = {"logger": "pgaudit", "injected": "private-marker-must-not-leak", "record": {
                "user_name": "postgres", "database_name": "chat", "application_name": marker,
                "query": "private-marker-must-not-leak", "audit": {
                    "audit_type": "SESSION", "class": "DDL", "object_type": "TABLE",
                    "object_name": "bravo_test." + marker, "command": "CREATE TABLE",
                    "statement": "private-marker-must-not-leak", "parameter": "private-marker-must-not-leak"}}}
            with source.open("a") as f:
                f.write("2026-11-03T00:00:00.000000000Z stdout F " + json.dumps(body) + "\n")
            return marker

        def present(marker):
            return any(marker in json.dumps(row) for _, row in records(root / "output/logs.json"))

        first = emit(1)
        wait_for(lambda: present(first))
        result["mtls_forwarding"] = True
        # No app-provided OTLP endpoint is open. Another Pod UID file must not be read.
        other = root / "input" / "audit-test_fake_33333333-3333-4333-8333-33333333333a" / "postgres"
        other.mkdir(parents=True)
        (other / "0.log").write_text(source.read_text().replace(first, "unregistered-source-marker"))
        # Receiver outage leaves records queued until restored.
        command("docker", "stop", "-t", "3", recv)
        pending = [emit(i) for i in range(2, 8)]
        time.sleep(4)
        command("docker", "kill", send)
        result["checkpoint_files_present"] = bool(list((root / "checkpoints").iterdir()))
        command("docker", "start", send)
        command("docker", "start", recv)
        wait_for(lambda: all(present(m) for m in pending))
        result["outage_and_sender_crash_recovery"] = True
        # Wrong server CA: the actual Collector must refuse export and retain the batch.
        command("docker", "stop", "-t", "3", send)
        shutil.copyfile(root / "tls/ca.crt", root / "tls/original-ca.crt")
        command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Wrong test CA", "-keyout", str(root / "tls/wrong.key"), "-out", str(root / "tls/ca.crt"))
        command("docker", "start", send)
        untrusted = emit(9)
        time.sleep(4)
        assert not present(untrusted)
        errors = subprocess.run(["docker", "logs", send], capture_output=True, text=True, timeout=10).stderr
        assert "certificate signed by unknown authority" in errors
        command("docker", "stop", "-t", "3", send)
        shutil.copyfile(root / "tls/original-ca.crt", root / "tls/ca.crt")
        command("docker", "start", send)
        wait_for(lambda: present(untrusted))
        result["collector_rejects_wrong_ca_and_recovers"] = True
        # A different issuer cannot authenticate as this collector to the receiver.
        command("docker", "stop", "-t", "3", send)
        shutil.copyfile(root / "tls/tls.crt", root / "tls/original-client.crt")
        command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Unregistered client issuer", "-keyout", str(root / "tls/client-ca.key"), "-out", str(root / "tls/client-ca.crt"))
        command("openssl", "x509", "-req", "-in", str(root / "tls/tls.csr"), "-CA", str(root / "tls/client-ca.crt"), "-CAkey", str(root / "tls/client-ca.key"), "-CAcreateserial", "-days", "1", "-extfile", str(root / "tls/openssl.cnf"), "-extensions", "ext", "-out", str(root / "tls/tls.crt"))
        command("docker", "start", send)
        unauthorized = emit(10)
        time.sleep(4)
        assert not present(unauthorized)
        errors = subprocess.run(["docker", "logs", send], capture_output=True, text=True, timeout=10).stderr
        assert "remote error: tls:" in errors
        command("docker", "stop", "-t", "3", send)
        shutil.copyfile(root / "tls/original-client.crt", root / "tls/tls.crt")
        command("docker", "start", send)
        wait_for(lambda: present(unauthorized))
        result["receiver_rejects_unregistered_client_issuer"] = True
        # Rename rotation must not silently skip the next source file.
        source.rename(logs / "0.log.1")
        source.touch()
        rotated = emit(8)
        wait_for(lambda: present(rotated))
        result["rename_rotation"] = True
        event = {"apiVersion": "audit.k8s.io/v1", "kind": "Event", "auditID": "audit-canary",
                 "stage": "ResponseComplete", "verb": "create", "user": {"username": "operator", "extra": {"sensitive": "private-marker-must-not-leak"}},
                 "objectRef": {"resource": "rolebindings", "namespace": "audit-test", "name": "bravo-audit-role"},
                 "responseStatus": {"code": 201, "message": "private-marker-must-not-leak"},
                 "requestObject": {"data": "private-marker-must-not-leak"}, "requestURI": "private-marker-must-not-leak"}
        audit.write_text(json.dumps(event) + "\n")
        wait_for(lambda: present("audit-canary"))
        data = (root / "output/logs.json").read_text()
        assert "private-marker-must-not-leak" not in data
        assert "unregistered-source-marker" not in data
        assert all(a["bravo.environment"] == "source" and a["bravo.audit.registration"] == bind.registration_digest for a, _ in records(root / "output/logs.json"))
        result.update(redaction=True, exact_pod_uid_scope=True, collector_bound_origin=True)
        result["received_records"] = len(records(root / "output/logs.json"))
        result["status"] = "PASS"
    finally:
        # Preserve diagnostic files, never print raw source/log contents.
        for kind, name in reversed(created):
            if kind == "container":
                p = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=10)
                (root / (name + ".log")).write_text(p.stdout + p.stderr)
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
            else:
                subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=20)
        (root / "result.json").write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.private_output.resolve()), indent=2))
