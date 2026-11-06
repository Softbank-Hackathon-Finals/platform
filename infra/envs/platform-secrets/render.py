"""Platform OpenBao lifecycle CR and Edge-only ESO delivery. No secret values."""
import argparse
import base64
import ipaddress
import json
import re
import ssl
from pathlib import Path

NAMESPACE = "platform-secrets"
NAME = "platform-openbao"
MOUNT = "edge-t17"
READER = "t17-edge-secret-reader"
WRITER = "t17-edge-key-writer"
AUDIENCE = "bravo-platform-edge-secrets"
IMAGE = "docker.io/openbao/openbao@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f"


def obj(kind, name, spec=None, *, namespace=NAMESPACE, api="v1", **fields):
    value = {"apiVersion": api, "kind": kind,
             "metadata": {"name": name, "labels": {"component": "platform"}}, **fields}
    if namespace:
        value["metadata"]["namespace"] = namespace
    if spec is not None:
        value["spec"] = spec
    return value


def foundation(node):
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", node):
        raise ValueError("verified_platform_node_required")
    ns = obj("Namespace", NAMESPACE, namespace=None)
    ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = "restricted"
    volume = obj("PersistentVolume", "platform-openbao-data", namespace=None, spec={
        "capacity": {"storage": "3Gi"}, "volumeMode": "Filesystem",
        "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "platform-local",
        "claimRef": {"namespace": NAMESPACE, "name": "data-platform-openbao-0"},
        "local": {"path": "/var/lib/bravo-platform/openbao"},
        "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{
            "key": "kubernetes.io/hostname", "operator": "In", "values": [node]}]}]}}})
    return {"apiVersion": "v1", "kind": "List", "items": [ns, volume,
        obj("ServiceAccount", WRITER, automountServiceAccountToken=False)]}


def cluster(api_endpoint):
    address = str(ipaddress.ip_address(api_endpoint))
    requests = [
        {"name": "audit", "operation": "update", "path": "sys/audit/stdout",
         "auditDevice": {"type": "file", "fileOptions": {"filePath": "stdout"}}},
        {"name": "edge-kv", "operation": "update", "path": "sys/mounts/" + MOUNT,
         "secretEngine": {"type": "kv", "options": {"version": "2"}}},
        {"name": "edge-cas", "operation": "update", "path": MOUNT + "/config",
         "data": {"cas_required": True, "max_versions": 5}},
    ]
    for role, namespace, policy in (
        (READER, "platform-state", 'path "edge-t17/data/cloudflare" { capabilities = ["read"] }'),
        (WRITER, NAMESPACE, ('path "edge-t17/data/cloudflare" { capabilities = ["create", "update"] }\n'
                            'path "edge-t17/metadata/cloudflare" { capabilities = ["read"] }')),
    ):
        requests += [
            {"name": role + "-policy", "operation": "update", "path": "sys/policies/acl/" + role,
             "policy": {"policy": policy}},
            {"name": role + "-role", "operation": "update", "path": "auth/jwt-operator/role/" + role,
             "data": {"role_type": "jwt", "user_claim": "sub", "bound_audiences": [AUDIENCE],
                      "bound_subject": f"system:serviceaccount:{namespace}:{role}",
                      "token_policies": [role], "token_no_default_policy": True,
                      "token_ttl": "300s", "token_max_ttl": "300s"}},
        ]
    return obj("OpenBaoCluster", NAME, api="openbao.org/v1alpha1", spec={
        "version": "2.7.2", "image": IMAGE, "replicas": 1,
        # Single-node hackathon platform. Static unseal is not a Hardened profile.
        "profile": "Development", "controllerJWTMode": "Target",
        "deletionPolicy": "Retain", "reconcilePolicies": True,
        "tls": {"enabled": True, "mode": "OperatorManaged", "rotationPeriod": "720h"},
        "unseal": {"type": "static"},
        "selfInit": {"enabled": True, "oidc": {"enabled": True}, "requests": requests},
        "storage": {"size": "3Gi", "storageClassName": "platform-local"},
        "configuration": {"ui": False, "logLevel": "info", "logging": {"format": "json"}},
        "resources": {"requests": {"cpu": "250m", "memory": "256Mi"},
                      "limits": {"cpu": "1", "memory": "512Mi"}},
        "network": {"apiServerEndpointIPs": [address], "ingressRules": [{
            "from": [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": "platform-external-secrets"}}}],
            "ports": [{"protocol": "TCP", "port": 8200}]}]},
    })


def delivery(ca_pem):
    # The operator's actual public CA is a protected deployment input, never a token.
    ssl.create_default_context(cadata=ca_pem)
    if "PRIVATE KEY" in ca_pem:
        raise ValueError("public_ca_required")
    store = obj("SecretStore", "t17-edge-openbao", namespace="platform-state",
                api="external-secrets.io/v1", spec={"provider": {"vault": {
                    "server": "https://platform-openbao.platform-secrets.svc:8200",
                    "path": MOUNT, "version": "v2",
                    "caBundle": base64.b64encode(ca_pem.encode()).decode(),
                    "auth": {"jwt": {"path": "jwt-operator", "role": READER,
                        "kubernetesServiceAccountToken": {"serviceAccountRef": {"name": READER},
                            "audiences": [AUDIENCE], "expirationSeconds": 600}}}}}})
    external = obj("ExternalSecret", "t17-cloudflare-lb", namespace="platform-state",
                   api="external-secrets.io/v1", spec={
        "refreshInterval": "30s", "secretStoreRef": {"name": "t17-edge-openbao", "kind": "SecretStore"},
        "target": {"name": "t17-cloudflare-lb", "creationPolicy": "Owner", "deletionPolicy": "Retain"},
        "data": [{"secretKey": "token", "remoteRef": {"key": "cloudflare", "property": "token"}}]})
    return {"apiVersion": "v1", "kind": "List", "items": [
        obj("ServiceAccount", READER, namespace="platform-state", automountServiceAccountToken=False),
        store, external]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("foundation", "cluster", "delivery"))
    parser.add_argument("--node")
    parser.add_argument("--api-endpoint")
    parser.add_argument("--public-ca", type=Path)
    args = parser.parse_args()
    if args.action == "foundation":
        result = foundation(args.node)
    elif args.action == "cluster":
        result = cluster(args.api_endpoint)
    else:
        result = delivery(args.public_ca.read_text())
    print(json.dumps(result, indent=2))
