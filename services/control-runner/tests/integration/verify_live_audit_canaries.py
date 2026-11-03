"""Opt-in actual CNPG/PostgreSQL/Kubernetes audit → Collector → T14 search.

Creates only a uniquely named local Docker k3s and two Collector containers.
No default kubeconfig, cloud credentials, published port or user workload.
Raw logs, generated credentials and TLS keys remain in private-output.
The receiver is a test file sink; this does not qualify AWS/RKE2 or retention.
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import verify_audit_collector as forwarding

from bravo_controls.audit_probes import AuditBatch, AuditSearchProbe, CanaryReceipt
from bravo_controls.otlp_audit import AuditOrigin, decode_export
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

K3S = "rancher/k3s:v1.35.9-k3s1@sha256:ec9868c6a38d4e8c1869832fb5fd1eb8473c39794a0b44d2b952e7ba911951bc"
CNPG = "1.30.1@sha256:923c267ec29636db3bee20f993d0ec4973fa22998e1adad37da79e4d32b5bc07"
PG = "ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185"
NS = "audit-test"
_ERROR_LOG = None


def command(*args, data=None, timeout=45):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        if _ERROR_LOG is not None:
            with _ERROR_LOG.open("a") as error_log:
                error_log.write(result.stderr + "\n")
        # Never include SQL, credentials or raw tool output in the public error.
        raise RuntimeError(f"{Path(args[0]).name}_failed_exit_{result.returncode}")
    return result.stdout


def until(predicate, *, seconds=180):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except RuntimeError:
            pass
        time.sleep(2)
    raise RuntimeError("bounded_readiness_timeout")


def run(root):
    global _ERROR_LOG
    _ERROR_LOG = root / "command-errors.log"
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("tls", "pods", "audit", "output", "checkpoints"):
        (root / name).mkdir()
    suffix = uuid4().hex[:12]
    cluster, sender, receiver, network = (
        "bravo-live-audit-" + suffix + s for s in ("-k3s", "-send", "-recv", "-net")
    )
    created = []
    result = {
        "status": "INCOMPLETE",
        "scope": "actual local k3s/CNPG audit and Collector; file receiver fixture",
        "cloud_resources_changed": False,
        "synthetic_audit_records": False,
        "images": {"k3s": K3S, "postgres": PG, "cnpg": CNPG, "collector": forwarding.IMAGE},
    }

    def k(*args, data=None, timeout=45):
        return command(
            "docker", "exec", "-i", cluster, "kubectl", *args, data=data, timeout=timeout
        )

    def apply(value):
        return k("apply", "-f", "-", data=json.dumps(value))

    def psql(sql, *, user="postgres", db="postgres", marker=None, password=None):
        args = ["-X", "-v", "ON_ERROR_STOP=1", "-At", "-U", user, "-d", db]
        if password is None:
            return k("-n", NS, "exec", "-i", "audit-db-1", "--", "psql", *args, data=sql)
        # Credential only in stdin to this disposable Pod; not argv or a file.
        script = (
            "IFS= read -r PGPASSWORD; export PGPASSWORD; "
            "export PGSSLMODE=verify-full PGSSLROOTCERT=/controller/certificates/server-ca.crt; "
            'export PGCONNECT_TIMEOUT=5 PGAPPNAME="$1"; shift; exec psql "$@"'
        )
        return k(
            "-n",
            NS,
            "exec",
            "-i",
            "audit-db-1",
            "--",
            "sh",
            "-c",
            script,
            "canary",
            marker,
            "-h",
            "audit-db-rw",
            *args,
            data=password + "\n" + sql,
        )

    try:
        policy = {
            "apiVersion": "audit.k8s.io/v1",
            "kind": "Policy",
            "omitStages": ["RequestReceived"],
            "rules": [
                {
                    "level": "Metadata",
                    "namespaces": [NS],
                    "resources": [
                        {"group": "rbac.authorization.k8s.io", "resources": ["rolebindings"]}
                    ],
                },
                {"level": "None"},
            ],
        }
        (root / "audit-policy.json").write_text(json.dumps(policy))
        command(
            "docker",
            "run",
            "-d",
            "--privileged",
            "--name",
            cluster,
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/var/run",
            "--memory",
            "3g",
            "--cpus",
            "3",
            "-v",
            f"{root}/pods:/var/log/pods",
            "-v",
            f"{root}/audit:/var/log/bravo-audit/kubernetes",
            "-v",
            f"{root}/audit-policy.json:/etc/audit-policy.json:ro",
            K3S,
            "server",
            "--disable=traefik",
            "--disable=metrics-server",
            "--disable=servicelb",
            "--kube-apiserver-arg=audit-policy-file=/etc/audit-policy.json",
            "--kube-apiserver-arg=audit-log-path=/var/log/bravo-audit/kubernetes/audit.log",
            "--kube-apiserver-arg=audit-log-maxsize=10",
            timeout=60,
        )
        created.append(("container", cluster))
        until(lambda: '"Ready"' in k("get", "nodes", "-o", "json"))
        k("wait", "--for=condition=Ready", "node", "--all", "--timeout=90s", timeout=95)
        k("create", "namespace", "cnpg-system")
        k("create", "namespace", NS)
        chart = command(
            "helm",
            "template",
            "audit-cnpg",
            "cnpg/cloudnative-pg",
            "--version",
            "0.29.1",
            "--namespace",
            "cnpg-system",
            "--set",
            f"image.tag={CNPG}",
            "--set",
            f"config.data.WATCH_NAMESPACE={NS}",
            timeout=90,
        )
        k("apply", "--server-side", "-f", "-", data=chart)
        k(
            "-n",
            "cnpg-system",
            "rollout",
            "status",
            "deployment/audit-cnpg-cloudnative-pg",
            "--timeout=180s",
            timeout=190,
        )
        apply(
            {
                "apiVersion": "postgresql.cnpg.io/v1",
                "kind": "Cluster",
                "metadata": {"name": "audit-db", "namespace": NS},
                "spec": {
                    "instances": 1,
                    "imageName": PG,
                    "storage": {"size": "1Gi"},
                    "postgresql": {
                        "shared_preload_libraries": ["pgaudit"],
                        "parameters": {
                            "log_connections": "on",
                            "pgaudit.log": "ddl",
                            "pgaudit.log_parameter": "off",
                        },
                    },
                },
            }
        )
        k(
            "-n",
            NS,
            "wait",
            "cluster/audit-db",
            "--for=condition=Ready",
            "--timeout=300s",
            timeout=310,
        )
        pod = json.loads(k("-n", NS, "get", "pod", "audit-db-1", "-o", "json"))
        node = json.loads(k("get", "node", pod["spec"]["nodeName"], "-o", "json"))
        identity = {
            "pod_uid": pod["metadata"]["uid"],
            "node_uid": node["metadata"]["uid"],
            "namespace": NS,
            "pod": "audit-db-1",
        }
        digest = (
            "sha256:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        )
        origin = AuditOrigin(
            "source", digest, identity["node_uid"], identity["pod_uid"], NS, "audit-db-1", True
        )
        password = secrets.token_hex(24)
        psql(
            f"CREATE ROLE audit_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD '{password}';\nCREATE DATABASE audit_db OWNER audit_role;\n"
        )
        psql(
            "CREATE EXTENSION IF NOT EXISTS pgaudit; CREATE SCHEMA bravo_test AUTHORIZATION audit_role; REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
            db="audit_db",
        )
        apply(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "audit-canary", "namespace": NS},
                "automountServiceAccountToken": False,
            }
        )
        apply(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "audit-empty", "namespace": NS},
                "rules": [],
            }
        )
        actor = json.loads(k("auth", "whoami", "-o", "json"))["status"]["userInfo"]["username"]
        context = RunContext(
            "actual-local-audit", "chat", Environment("source", "source", "docker:owned-k3s")
        )
        receipts = {}
        for test_id in ("AUD-01.1", "AUD-01.2", "AUD-01.3"):
            marker = "bravo_audit_" + uuid4().hex
            started = datetime.now(UTC)
            if test_id == "AUD-01.1":
                assert (
                    psql(
                        "SELECT current_user;",
                        user="audit_role",
                        db="audit_db",
                        marker=marker,
                        password=password,
                    ).strip()
                    == "audit_role"
                )
                acknowledged = frozenset({"login"})
            elif test_id == "AUD-01.2":
                psql(
                    f"CREATE TABLE bravo_test.{marker}(id integer PRIMARY KEY); DROP TABLE bravo_test.{marker};",
                    user="audit_role",
                    db="audit_db",
                    marker=marker,
                    password=password,
                )
                acknowledged = frozenset({"create", "delete"})
            else:
                binding = {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": marker, "namespace": NS},
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "Role",
                        "name": "audit-empty",
                    },
                    "subjects": [
                        {"kind": "ServiceAccount", "name": "audit-canary", "namespace": NS}
                    ],
                }
                k("create", "-f", "-", data=json.dumps(binding))
                k("-n", NS, "delete", "rolebinding", marker, "--wait=true", "--timeout=10s")
                acknowledged = frozenset({"create", "delete"})
            receipts[test_id] = CanaryReceipt(
                context,
                test_id,
                marker,
                actor if test_id == "AUD-01.3" else "audit_role",
                "audit_db",
                NS,
                "bravo_test",
                started,
                datetime.now(UTC),
                started + timedelta(minutes=3),
                acknowledged,
            )
        (root / "canary-receipts.json").write_text(
            json.dumps(
                {t: asdict(r) for t, r in receipts.items()},
                default=lambda v: sorted(v) if isinstance(v, frozenset) else v.isoformat(),
                indent=2,
            )
        )
        # Actual CRI and apiserver files are mounted from the owned cluster;
        # no generated replacement audit messages enter the sender.
        forwarding.certificates(root / "tls")
        registration = forwarding.AuditForwardingRegistration(
            "source",
            "source",
            digest,
            NS,
            "audit-db-1",
            origin.pod_uid,
            origin.node_uid,
            "https://audit-receiver:8443",
            True,
        )
        (root / "sender.json").write_text(json.dumps(forwarding.collector_config(registration)))
        config = {
            "receivers": {
                "otlp": {
                    "protocols": {
                        "http": {
                            "endpoint": "0.0.0.0:8443",
                            "tls": {
                                "cert_file": "/tls/tls.crt",
                                "key_file": "/tls/tls.key",
                                "client_ca_file": "/tls/ca.crt",
                                "min_version": "1.2",
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
        (root / "receiver.json").write_text(json.dumps(config))
        command("docker", "network", "create", "--internal", network)
        created.append(("network", network))
        common = [
            "docker",
            "run",
            "-d",
            "--network",
            network,
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
        command(
            *common,
            "--name",
            receiver,
            "--network-alias",
            "audit-receiver",
            "-v",
            f"{root}/receiver.json:/etc/config.json:ro",
            "-v",
            f"{root}/tls:/tls:ro",
            "-v",
            f"{root}/output:/output",
            forwarding.IMAGE,
            "--config=/etc/config.json",
        )
        created.append(("container", receiver))
        command(
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
            f"{root}/pods:/var/log/pods:ro",
            "-v",
            f"{root}/audit:/var/log/bravo-audit/kubernetes:ro",
            forwarding.IMAGE,
            "--config=/etc/config.json",
        )
        created.append(("container", sender))
        exported = root / "output/logs.json"

        def found():
            if not exported.exists():
                return False
            raw = exported.read_text()
            return all(r.marker in raw for r in receipts.values())

        until(found, seconds=45)
        command("docker", "stop", "-t", "3", sender)
        command("docker", "stop", "-t", "3", receiver)
        received = datetime.now(UTC)
        raw = exported.read_bytes()
        assert password.encode() not in raw
        events = tuple(
            e
            for i, line in enumerate(raw.splitlines())
            for e in decode_export(
                line, origin=origin, received_at=received, receipt_id=f"local-receipt-{i}"
            )
        )
        results = {}
        for test_id, receipt in receipts.items():
            probe = AuditSearchProbe(
                receipt_provider=lambda c, s, r=receipt: r,
                search=lambda r, through: AuditBatch(
                    context,
                    r.marker,
                    digest,
                    origin.streams,
                    r.started_at,
                    through,
                    received,
                    tuple(e for e in events if r.started_at <= e.timestamp <= through),
                    True,
                ),
                source_binding=digest,
                streams=origin.streams,
                evidence_sink=lambda data, t=test_id: (root / (t + ".json")).write_bytes(data),
                clock=lambda: received,
            )
            results[test_id] = probe(
                context, Spec(test_id, "AUD-01", "1", "present", "not_applicable")
            ).observed
        result.update(
            results=results,
            decoded_events=len(events),
            credential_redacted=True,
            collected_at=received.isoformat(),
        )
        assert results == {t: "present" for t in receipts}, "actual_canary_search_failed"
        result["status"] = "PASS"
    finally:
        cleanup = []
        for kind, name in reversed(created):
            if kind == "container":
                logs = subprocess.run(
                    ["docker", "logs", name], capture_output=True, text=True, timeout=15
                )
                (root / (name + ".log")).write_text(logs.stdout + logs.stderr)
            try:
                command("docker", kind, "rm", *(["-f"] if kind == "container" else []), name)
                cleanup.append(True)
            except Exception:
                cleanup.append(False)
        result["owned_resources_removed"] = all(cleanup)
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.private_output.resolve()), indent=2))
