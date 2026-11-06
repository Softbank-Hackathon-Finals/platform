"""Render a registration host from private references and observed egress addresses."""

import argparse
import hashlib
import ipaddress
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "catalog", "grants", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--gate-grants", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("new_output_required")
    inputs = json.loads(args.inputs.read_text())
    cfg = json.loads(args.catalog.read_text())
    grants = json.loads(args.grants.read_text())
    (release,) = cfg["releases"]
    image = inputs["image"]
    if re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) is None:
        raise ValueError("immutable_image_required")
    if re.fullmatch(r"[a-f0-9]{40}", inputs["source_revision"]) is None:
        raise ValueError("source_revision_required")
    for field in ("collector_secret", "tls_secret"):
        if re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", inputs[field]) is None:
            raise ValueError("invalid_secret_reference")
    labels = {"app.kubernetes.io/name": "t10-registration", "component": "platform"}

    def meta(name):
        return {"name": name, "namespace": "platform-state", "labels": labels}

    data = {
        "catalog.json": json.dumps(cfg, sort_keys=True),
        "grants.json": json.dumps(grants, sort_keys=True),
    }
    if ("gate_records" in cfg) != bool(args.gate_grants):
        raise ValueError("explicit_gate_record_grants_required")
    if args.gate_grants:
        if cfg["gate_records"] != {"grants_file": "/registration/gate-grants.json"}:
            raise ValueError("fixed_gate_record_grant_mount_required")
        data["gate-grants.json"] = json.dumps(json.loads(args.gate_grants.read_text()), sort_keys=True)
    config_digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    name = "t10-registration-config-" + config_digest[:16]
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": meta(name),
        "immutable": True,
        "data": data,
    }
    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": meta("t10-registration"),
        "automountServiceAccountToken": False,
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": meta("t10-registration"),
        "spec": {
            "type": "ClusterIP",
            "selector": labels,
            "ports": [{"name": "https", "port": 8443, "targetPort": "https"}],
        },
    }
    hosts = [
        "sts." + cfg["sso_verifier"]["region"] + ".amazonaws.com",
        "github.com",
        cfg["environments"][0]["endpoint"].removeprefix("https://"),
    ]
    addresses = inputs["egress_ipv4"]
    if set(addresses) != set(hosts):
        raise ValueError("exact_registered_hosts_required")
    for values in addresses.values():
        if not isinstance(values, list) or not values or len(set(values)) != len(values):
            raise ValueError("resolved_addresses_required")
        for value in values:
            address = ipaddress.IPv4Address(value)
            if (
                str(address) != value
                or address.is_loopback
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValueError("invalid_egress_address")
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": meta("t10-registration-trusted-egress"),
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "t10-registration"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {"ipBlock": {"cidr": ip + "/32"}}
                        for host in hosts
                        for ip in addresses[host]
                    ],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                }
            ],
        },
    }

    def secret(name, keys):
        return {
            "secretName": name,
            "defaultMode": 288,
            "items": [{"key": k, "path": k} for k in keys],
        }

    volumes = [
        {"name": "credential", "secret": secret("platform-runtime-db", ["username", "password"])},
        {"name": "ca", "secret": secret("platform-db-ca", ["ca.crt"])},
        {"name": "registration", "configMap": {"name": name, "defaultMode": 292}},
        {
            "name": "collectors",
            "secret": secret(
                inputs["collector_secret"],
                ["source-token", "target-token", "source-ca.crt", "target-ca.crt"],
            ),
        },
        {"name": "tls", "secret": secret(inputs["tls_secret"], ["tls.crt", "tls.key"])},
        {"name": "checkout", "emptyDir": {"sizeLimit": "64Mi"}},
    ]
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    # The init container receives no runtime DB/collector/TLS mounts and fetches one fixed approved commit.
    script = (
        "import subprocess; p='/checkout/repo'; run=lambda a:subprocess.run(['git',*a],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60);run(['init',p]);run(['-C',p,'remote','add','origin',"
        + repr(release["repo_url"])
        + "]);run(['-C',p,'fetch','--depth=1','origin',"
        + repr(release["revision"])
        + "]);assert subprocess.check_output(['git','-C',p,'rev-parse','FETCH_HEAD'],text=True).strip()=="
        + repr(release["revision"])
    )
    init = {
        "name": "approved-source",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-c", script],
        "env": [
            {"name": "HOME", "value": "/nonexistent"},
            {"name": "GIT_TERMINAL_PROMPT", "value": "0"},
            {"name": "GIT_CONFIG_NOSYSTEM", "value": "1"},
        ],
        "securityContext": security,
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"},
        },
        "volumeMounts": [{"name": "checkout", "mountPath": "/checkout"}],
    }
    container = {
        "name": "registration",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "env": [
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "PYTHONUNBUFFERED", "value": "1"},
        ],
        "ports": [{"name": "https", "containerPort": 8443}],
        "securityContext": security,
        "resources": {
            "requests": {"cpu": "100m", "memory": "192Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },
        "startupProbe": {
            "tcpSocket": {"port": "https"},
            "periodSeconds": 5,
            "failureThreshold": 12,
        },
        "readinessProbe": {"tcpSocket": {"port": "https"}, "periodSeconds": 5},
        "volumeMounts": [
            {"name": v["name"], "mountPath": "/" + v["name"], "readOnly": True} for v in volumes
        ],
    }
    deploy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta("t10-registration"),
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": dict(
                        labels,
                        **{
                            "bravo.dev/db-client": "runtime",
                            "bravo.dev/target-api-client": "collector",
                        },
                    ),
                    "annotations": {
                        "bravo.dev/source-revision": inputs["source_revision"],
                        "bravo.dev/config-sha256": config_digest,
                    },
                },
                "spec": {
                    "hostAliases": [
                        {"ip": addresses[host][0], "hostnames": [host.lower()]} for host in hosts
                    ],
                    "serviceAccountName": "t10-registration",
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 25,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": [init],
                    "containers": [container],
                    "volumes": volumes,
                },
            },
        },
    }
    items = [sa, cm, policy, service, deploy]
    args.output.write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "objects": [x["kind"] + "/" + x["metadata"]["name"] for x in items],
                "image": image,
                "secret_values": False,
                "runtime_admin_credentials": False,
            }
        )
    )


if __name__ == "__main__":
    main()
