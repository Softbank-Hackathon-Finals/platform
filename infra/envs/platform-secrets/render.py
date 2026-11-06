"""Platform OpenBao lifecycle CR and ESO delivery for every platform credential.

Human operator access (SSO -> SSM) stays outside OpenBao. Every other platform
credential is a KV v2 entry here, written by a dedicated writer identity and
read by exactly one consumer ServiceAccount through ESO. No secret values.
"""
import argparse
import base64
import ipaddress
import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

NAMESPACE = "platform-secrets"
NAME = "platform-openbao"
MOUNT = "platform"
CONSUMER_NAMESPACE = "platform-state"
AUDIENCE = "bravo-platform-secrets"
SERVER = "https://platform-openbao.platform-secrets.svc:8200"
IMAGE = "ghcr.io/softbank-hackathon-finals/platform-openbao@sha256:c85ac2d5f89ac756c3e0b448377ce125b007e9563b03ceb31afafafa3df12bec"
NAME_RE = r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"


@dataclass(frozen=True)
class Credential:
    """One KV entry, one reader ServiceAccount, one writer ServiceAccount."""

    key: str
    reader: str
    writer: str
    target: str
    properties: tuple
    writer_namespace: str = NAMESPACE

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)+", self.key):
            raise ValueError("credential_key_invalid")
        for name in (self.reader, self.writer, self.target):
            if not re.fullmatch(NAME_RE, name):
                raise ValueError("credential_name_invalid")
        if not self.properties or any(not re.fullmatch(r"[a-z][a-z0-9_]*", p) for p in self.properties):
            raise ValueError("credential_properties_invalid")


# The platform credential catalog. Adding an entry before the first
# initialization creates its policy and roles; afterwards see README.
CREDENTIALS = (
    # T17 Edge weight changes: one Cloudflare LB, PATCH/GET only.
    Credential("edge/cloudflare-lb", reader="t17-edge-secret-reader",
               writer="t17-edge-key-writer", target="t17-cloudflare-lb", properties=("token",)),
    # Cloudflare GitOps reconciler: Tunnel, pool, LB and DNS declared in Git.
    Credential("edge/cloudflare-gitops", reader="cloudflare-gitops-reader",
               writer="cloudflare-gitops-key-writer", target="cloudflare-gitops-api",
               properties=("token", "account_id", "zone_id")),
    # Cloudflare Tunnel connector tokens, one per environment, written by the reconciler.
    Credential("edge/tunnel-source-dev", reader="edge-tunnel-source-reader",
               writer="cloudflare-gitops-reader", target="edge-tunnel-source-dev",
               properties=("token",), writer_namespace=CONSUMER_NAMESPACE),
    Credential("edge/tunnel-target-dev", reader="edge-tunnel-target-reader",
               writer="cloudflare-gitops-reader", target="edge-tunnel-target-dev",
               properties=("token",), writer_namespace=CONSUMER_NAMESPACE),
    # AI proposal CronJob: short-lived model access token only.
    Credential("ai/model-access", reader="ai-proposal-runner",
               writer="ai-subscription-refresher", target="ai-model-access",
               properties=("access_token", "account_id", "expires_at"),
               writer_namespace=CONSUMER_NAMESPACE),
    # Subscription refresh token: read and rotated only by the refresher.
    Credential("ai/model-refresh", reader="ai-subscription-refresher",
               writer="ai-subscription-refresher", target="ai-model-refresh",
               properties=("client_id", "refresh_token"), writer_namespace=CONSUMER_NAMESPACE),
)


def obj(kind, name, spec=None, *, namespace=NAMESPACE, api="v1", **fields):
    value = {"apiVersion": api, "kind": kind,
             "metadata": {"name": name, "labels": {"component": "platform"}}, **fields}
    if namespace:
        value["metadata"]["namespace"] = namespace
    if spec is not None:
        value["spec"] = spec
    return value


def _role(name, namespace, policy):
    return [
        {"name": name + "-policy", "operation": "update", "path": "sys/policies/acl/" + name,
         "policy": {"policy": policy}},
        {"name": name + "-role", "operation": "update", "path": "auth/jwt-operator/role/" + name,
         "data": {"role_type": "jwt", "user_claim": "sub", "bound_audiences": [AUDIENCE],
                  "bound_subject": f"system:serviceaccount:{namespace}:{name}",
                  "token_policies": [name], "token_no_default_policy": True,
                  "token_ttl": "300s", "token_max_ttl": "300s"}},
    ]


def policies(catalog=CREDENTIALS):
    """Exact-path ACLs keyed by (namespace, ServiceAccount). No wildcards."""
    rules = {}
    for entry in catalog:
        data, metadata = f"{MOUNT}/data/{entry.key}", f"{MOUNT}/metadata/{entry.key}"
        rules.setdefault((CONSUMER_NAMESPACE, entry.reader), []).append(
            f'path "{data}" {{ capabilities = ["read"] }}')
        rules.setdefault((entry.writer_namespace, entry.writer), []).extend([
            f'path "{data}" {{ capabilities = ["create", "update"] }}',
            f'path "{metadata}" {{ capabilities = ["read"] }}'])
    names = [name for _, name in rules]
    if len(names) != len(set(names)):
        raise ValueError("serviceaccount_name_reused_across_namespaces")
    return {key: "\n".join(dict.fromkeys(lines)) for key, lines in rules.items()}


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
    writers = sorted({e.writer for e in CREDENTIALS if e.writer_namespace == NAMESPACE})
    return {"apiVersion": "v1", "kind": "List", "items": [ns, volume] + [
        obj("ServiceAccount", name, automountServiceAccountToken=False) for name in writers]}


def cluster(api_endpoint):
    address = str(ipaddress.ip_address(api_endpoint))
    requests = [
        {"name": "platform-kv", "operation": "update", "path": "sys/mounts/" + MOUNT,
         "secretEngine": {"type": "kv", "options": {"version": "2"}}},
        {"name": "platform-cas", "operation": "update", "path": MOUNT + "/config",
         "data": {"cas_required": True, "max_versions": 5}},
    ]
    for (namespace, name), policy in policies().items():
        requests += _role(name, namespace, policy)
    return obj("OpenBaoCluster", NAME, api="openbao.org/v1alpha1", spec={
        "version": "2.7.2", "image": IMAGE, "replicas": 1,
        "imagePullSecrets": [{"name": "platform-ghcr-reader"}],
        # OpenBao 2.7 rejects audit devices through the API; declare them here.
        "audit": [{"type": "file", "path": "stdout",
                   "fileOptions": {"filePath": "stdout"}}],
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
                "kubernetes.io/metadata.name": ns}}}],
            "ports": [{"protocol": "TCP", "port": 8200}]}
            for ns in ("platform-external-secrets", CONSUMER_NAMESPACE)]},
    })


def delivery(ca_pem, catalog=CREDENTIALS):
    """Per-consumer SecretStore bound to that consumer's own OpenBao role."""
    # The operator's actual public CA is a protected deployment input, never a token.
    ssl.create_default_context(cadata=ca_pem)
    if "PRIVATE KEY" in ca_pem:
        raise ValueError("public_ca_required")
    ca = base64.b64encode(ca_pem.encode()).decode()
    items, accounts = [], set()
    for entry in catalog:
        if entry.reader not in accounts:
            accounts.add(entry.reader)
            items += [
                obj("ServiceAccount", entry.reader, namespace=CONSUMER_NAMESPACE,
                    automountServiceAccountToken=False),
                obj("SecretStore", entry.reader, namespace=CONSUMER_NAMESPACE,
                    api="external-secrets.io/v1", spec={"provider": {"vault": {
                        "server": SERVER, "path": MOUNT, "version": "v2", "caBundle": ca,
                        "auth": {"jwt": {"path": "jwt-operator", "role": entry.reader,
                            "kubernetesServiceAccountToken": {
                                "serviceAccountRef": {"name": entry.reader},
                                "audiences": [AUDIENCE], "expirationSeconds": 600}}}}}}),
            ]
        items.append(obj("ExternalSecret", entry.target, namespace=CONSUMER_NAMESPACE,
                         api="external-secrets.io/v1", spec={
            "refreshInterval": "30s",
            "secretStoreRef": {"name": entry.reader, "kind": "SecretStore"},
            "target": {"name": entry.target, "creationPolicy": "Owner", "deletionPolicy": "Retain"},
            "data": [{"secretKey": prop, "remoteRef": {"key": entry.key, "property": prop}}
                     for prop in entry.properties]}))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def catalog_summary():
    """Reviewable map of who can read and write each credential."""
    return [{"key": f"{MOUNT}/{e.key}", "reader": f"{CONSUMER_NAMESPACE}/{e.reader}",
             "writer": f"{e.writer_namespace}/{e.writer}", "secret": f"{CONSUMER_NAMESPACE}/{e.target}",
             "properties": list(e.properties)} for e in CREDENTIALS]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("foundation", "cluster", "delivery", "catalog"))
    parser.add_argument("--node")
    parser.add_argument("--api-endpoint")
    parser.add_argument("--public-ca", type=Path)
    args = parser.parse_args()
    if args.action == "foundation":
        result = foundation(args.node)
    elif args.action == "cluster":
        result = cluster(args.api_endpoint)
    elif args.action == "delivery":
        result = delivery(args.public_ca.read_text())
    else:
        result = catalog_summary()
    print(json.dumps(result, indent=2))
