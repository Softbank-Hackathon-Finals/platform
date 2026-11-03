#!/usr/bin/env python3
"""Render bounded probe Jobs; pass/fail comes from the observed Job output."""
import argparse
import ipaddress
import json
import re
from pathlib import Path

from render import LABELS, POSTGRES_IMAGE, resource

APP_IMAGE = "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4"


def probe(stage, run_suffix="", pooler_ip=None):
    if stage not in {"db", "objects-bootstrap", "objects-write", "objects-read", "image", "pooler"}:
        raise ValueError("known probe stage required")
    name = "target-probe-" + stage
    if run_suffix:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,19}", run_suffix):
            raise ValueError("short DNS-safe run suffix required")
        name += "-" + run_suffix
    uid = 26 if stage == "db" else 10001
    labels = {**LABELS, "bravo-probe": name}
    if stage.startswith("objects-"):
        labels["bravo-object-client"] = "true"
    env = [{"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
           {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
    volumes, mounts, items = [], [], []
    if stage == "image":
        command = ["/app/.venv/bin/python", "-c", "import json,os,app.main,psycopg.pq; assert os.getuid()==10001; "
                   "print(json.dumps({'pass':True,'uid':os.getuid(),'app_import':True,"
                   "'libpq_version':psycopg.pq.version(),'app_startup_verified':False}))"]
    else:
        script = "db_probe.py" if stage == "db" else "pooler_probe.py" if stage == "pooler" else "object_probe.py"
        config = resource("v1", "ConfigMap", name, data={script: Path(__file__).with_name(script).read_text()})
        items.append(config)
        volumes.append({"name": "script", "configMap": {"name": name}})
        mounts.append({"name": "script", "mountPath": "/probe", "readOnly": True})
        command = ["python3" if stage == "db" else "/app/.venv/bin/python", "/probe/" + script]
        if stage not in {"db", "pooler"}:
            command.append(stage.split("-", 1)[1])
    if stage == "db":
        for key, secret in [("REPLICATION_PASSWORD", "target-replication"), ("APP_PASSWORD", "target-app-db")]:
            env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": secret, "key": "password"}}})
        volumes.append({"name": "ca", "secret": {"secretName": "target-db-ca",
                                                   "items": [{"key": "ca.crt", "path": "ca.crt"}]}})
        mounts.append({"name": "ca", "mountPath": "/tls", "readOnly": True})
    elif stage == "pooler":
        address = ipaddress.IPv4Address(pooler_ip)
        if not address.is_private or address.is_loopback or address.is_link_local:
            raise ValueError("verified target pooler Service address required")
        env.extend([{"name": "APP_PASSWORD", "valueFrom": {"secretKeyRef": {
            "name": "target-pooler-source-app", "key": "password"}}},
            {"name": "POOLER_IP", "value": str(address)}])
        volumes.extend([{"name": "ca", "secret": {"secretName": "target-db-ca",
            "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
            {"name": "auth", "secret": {"secretName": "target-pgbouncer-auth", "defaultMode": 288}}])
        mounts.extend([{"name": "ca", "mountPath": "/tls", "readOnly": True},
                       {"name": "auth", "mountPath": "/auth", "readOnly": True}])
    elif stage.startswith("objects-"):
        secret = "target-object-bootstrap" if stage == "objects-bootstrap" else "target-object-client"
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]:
            env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}})
    job = resource("batch/v1", "Job", name, spec={
        "backoffLimit": 0, "activeDeadlineSeconds": 600,
        "template": {"metadata": {"labels": labels}, "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
                                "fsGroup": uid, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "probe", "image": POSTGRES_IMAGE if stage == "db" else APP_IMAGE,
                "imagePullPolicy": "IfNotPresent" if stage == "db" else "Never",
                "command": command, "env": env, "volumeMounts": mounts,
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                              "limits": {"cpu": "500m", "memory": "256Mi"}},
            }], "volumes": volumes,
        }},
    })
    if stage == "image":
        items.append(resource("networking.k8s.io/v1", "NetworkPolicy", name, spec={
            "podSelector": {"matchLabels": {"bravo-probe": name}},
            "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [],
        }))
    return [*items, job]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["db", "objects-bootstrap", "objects-write", "objects-read", "image", "pooler"])
    parser.add_argument("--run-suffix", default="")
    parser.add_argument("--pooler-ip")
    args = parser.parse_args()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": probe(args.stage, args.run_suffix, args.pooler_ip)}, indent=2))
