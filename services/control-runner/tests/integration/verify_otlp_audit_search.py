"""Opt-in actual Collector transport with synthetic login/DDL/RBAC audit records.

Requires T34 audit_forwarding. No AWS credentials, host ports or socket mounts.
The file receiver and synthetic canary receipts are test fixtures, not production
storage or proof that a real DB/API emitted these particular records.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from bravo_controls.audit_probes import AuditBatch, AuditSearchProbe, CanaryReceipt
from bravo_controls.otlp_audit import AuditOrigin, decode_export
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec


def run(root, qualification_harness):
    # Reuse the separately reviewed forwarding qualification's pinned image,
    # certificate fixture and subprocess bounds. The path is operator-selected.
    module = importlib.util.spec_from_file_location("collector_fixture", qualification_harness)
    fixture = importlib.util.module_from_spec(module)
    module.loader.exec_module(fixture)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    for folder in ("tls", "checkpoints", "output", "input", "audit"):
        (root / folder).mkdir()
    fixture.certificates(root / "tls")
    origin = AuditOrigin(
        "source",
        "sha256:" + "a" * 64,
        "22222222-2222-4222-8222-222222222222",
        "11111111-1111-4111-8111-111111111111",
        "audit-test",
        "audit-db-1",
        True,
    )
    binding = fixture.AuditForwardingRegistration(
        origin.environment,
        origin.environment,
        origin.registration_digest,
        origin.namespace,
        origin.pod,
        origin.pod_uid,
        origin.node_uid,
        "https://audit-receiver:8443",
        True,
    )
    (root / "sender.json").write_text(json.dumps(fixture.collector_config(binding)))
    receiver = {
        "receivers": {
            "otlp": {
                "protocols": {
                    "http": {
                        "endpoint": "0.0.0.0:8443",
                        "tls": {
                            "cert_file": "/tls/tls.crt",
                            "key_file": "/tls/tls.key",
                            "client_ca_file": "/tls/ca.crt",
                        },
                    }
                }
            }
        },
        "exporters": {
            "file": {"path": "/output/logs.json", "append": True, "flush_interval": "100ms"}
        },
        "service": {"pipelines": {"logs": {"receivers": ["otlp"], "exporters": ["file"]}}},
    }
    (root / "receiver.json").write_text(json.dumps(receiver))
    podpath = root / "input" / f"{origin.namespace}_{origin.pod}_{origin.pod_uid}" / "postgres"
    podpath.mkdir(parents=True)
    source = podpath / "0.log"
    source.touch()
    audit = root / "audit" / "audit.log"
    audit.touch()
    suffix = uuid4().hex[:12]
    net, sender, recv = ("bravo-audit-search-" + suffix + s for s in ("-net", "-send", "-recv"))
    created = []
    result = {
        "scope": "actual Collector; synthetic canaries; file receiver fixture",
        "image": fixture.IMAGE,
    }
    try:
        fixture.command("docker", "network", "create", "--internal", net)
        created.append(("network", net))
        common = [
            "docker",
            "run",
            "-d",
            "--network",
            net,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            "256m",
            "--cpus",
            "1",
        ]
        fixture.command(
            *common,
            "--name",
            recv,
            "--network-alias",
            "audit-receiver",
            "-v",
            f"{root}/receiver.json:/etc/config.json:ro",
            "-v",
            f"{root}/tls:/tls:ro",
            "-v",
            f"{root}/output:/output",
            fixture.IMAGE,
            "--config=/etc/config.json",
        )
        created.append(("container", recv))
        fixture.command(
            *common,
            "--name",
            sender,
            "-v",
            f"{root}/sender.json:/etc/config.json:ro",
            "-v",
            f"{root}/tls:/var/run/bravo-audit/tls:ro",
            "-v",
            f"{root}/checkpoints:/var/lib/bravo-audit/checkpoints",
            "-v",
            f"{root}/input:/var/log/pods:ro",
            "-v",
            f"{root}/audit:/var/log/bravo-audit/kubernetes:ro",
            fixture.IMAGE,
            "--config=/etc/config.json",
        )
        created.append(("container", sender))
        started = datetime.now(UTC)
        context = RunContext(
            "actual-collector-search",
            "chat",
            Environment("source", "source", "docker:audit-fixture"),
        )
        receipts = {}
        for test_id in ("AUD-01.1", "AUD-01.2", "AUD-01.3"):
            marker = "bravo_audit_" + uuid4().hex
            actions = {"login"} if test_id == "AUD-01.1" else {"create", "delete"}
            for action in sorted(actions):
                now = datetime.now(UTC)
                if test_id == "AUD-01.3":
                    body = {
                        "apiVersion": "audit.k8s.io/v1",
                        "kind": "Event",
                        "stage": "ResponseComplete",
                        "auditID": str(uuid4()),
                        "verb": action,
                        "user": {"username": "audit_role"},
                        "objectRef": {
                            "apiGroup": "rbac.authorization.k8s.io",
                            "resource": "rolebindings",
                            "namespace": origin.namespace,
                            "name": marker,
                        },
                        "responseStatus": {"code": 201 if action == "create" else 200},
                        "stageTimestamp": now.isoformat(),
                        "requestObject": {"secret": "must-redact-this"},
                    }
                    with audit.open("a") as output:
                        output.write(json.dumps(body) + "\n")
                else:
                    record = {
                        "user_name": "audit_role",
                        "database_name": "chat",
                        "application_name": marker,
                        "query": "must-redact-this",
                    }
                    if action == "login":
                        record.update(
                            error_severity="LOG",
                            message=f"connection authorized: user=audit_role database=chat application_name={marker}",
                        )
                    else:
                        record["audit"] = {
                            "audit_type": "SESSION",
                            "class": "DDL",
                            "object_type": "TABLE",
                            "object_name": "bravo_test." + marker,
                            "command": "CREATE TABLE" if action == "create" else "DROP TABLE",
                        }
                    with source.open("a") as output:
                        output.write(
                            now.isoformat().replace("+00:00", "Z")
                            + " stdout F "
                            + json.dumps(
                                {
                                    "logger": "postgres" if action == "login" else "pgaudit",
                                    "record": record,
                                }
                            )
                            + "\n"
                        )
            receipts[test_id] = CanaryReceipt(
                context,
                test_id,
                marker,
                "audit_role",
                "chat",
                origin.namespace,
                "bravo_test",
                started,
                datetime.now(UTC),
                started + timedelta(minutes=2),
                frozenset(actions),
            )
        exported = root / "output/logs.json"
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if len(fixture.records(exported)) == 5:
                break
            time.sleep(0.5)
        assert len(fixture.records(exported)) == 5
        # Stop the file writer before reading a complete retained export. This
        # local observation time is deliberately later than every received event.
        fixture.command("docker", "stop", "-t", "3", recv)
        received = datetime.now(UTC)
        raw = exported.read_bytes()
        assert b"must-redact-this" not in raw
        events = tuple(
            event
            for i, line in enumerate(raw.splitlines())
            for event in decode_export(
                line, origin=origin, received_at=received, receipt_id=f"retained-export-{i}"
            )
        )
        collected = datetime.now(UTC)
        results = {}
        for test_id, receipt in receipts.items():
            probe = AuditSearchProbe(
                receipt_provider=lambda c, s, r=receipt: r,
                search=lambda r, through: AuditBatch(
                    context,
                    r.marker,
                    origin.registration_digest,
                    origin.streams,
                    r.started_at,
                    through,
                    collected,
                    events,
                    True,
                ),
                source_binding=origin.registration_digest,
                streams=origin.streams,
                evidence_sink=lambda data, t=test_id: (root / (t + ".json")).write_bytes(data),
                clock=lambda: collected,
            )
            results[test_id] = probe(
                context, Spec(test_id, "AUD-01", "1", "present", "not_applicable")
            ).observed
        assert results == {t: "present" for t in receipts}
        result.update(
            results=results,
            redaction=True,
            decoded_events=len(events),
            status="PASS",
            collected_at=collected.isoformat(),
        )
    finally:
        for kind, name in reversed(created):
            if kind == "container":
                logs = subprocess.run(
                    ["docker", "logs", name], capture_output=True, text=True, timeout=10
                )
                (root / (name + ".log")).write_text(logs.stdout + logs.stderr)
            fixture.command("docker", kind, "rm", *(["-f"] if kind == "container" else []), name)
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-output", required=True, type=Path)
    parser.add_argument("--forwarding-harness", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(run(args.private_output.resolve(), args.forwarding_harness.resolve()), indent=2)
    )
