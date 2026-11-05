"""Render isolated T15/T31 API routes and target apply RBAC. Never apply or mint credentials."""

import argparse
import json
from pathlib import PurePosixPath
import re
import sys

ROLES = ("rehearsal", "apply")
SYSTEM = "tailscale-system"
TAILNET = "bravo-clusters"
PROXY_CLASS = "bravo-clusters-egress-v11044"
PROXY_IMAGE = "tailscale/tailscale:v1.104.4@sha256:ed6480b33c6eb67c9cbd203ed42b9fb57e361986725b6856def71b7a3d9e276c"
CLIENT_LABEL = "bravo.dev/target-api-client"
MANAGED = {"app.kubernetes.io/part-of": "bravo-target-access"}


def name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value
    ):
        raise ValueError("DNS label required")
    return value


def hostname(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.ts\.net",
        value,
    ):
        raise ValueError("verified canonical MagicDNS service hostname required")
    return value


def control(role):
    if role not in ROLES:
        raise ValueError("unknown role")
    return "bravo-" + role + "-control"


def group(role):
    control(role)
    return "bravo-" + role + "-egress"


def meta(value, namespace=None):
    return {
        "name": name(value),
        "labels": MANAGED.copy(),
        **({"namespace": name(namespace)} if namespace else {}),
    }


def proxy_labels(role):
    return {
        "tailscale.com/managed": "true",
        "tailscale.com/parent-resource": group(role),
        "tailscale.com/parent-resource-type": "proxygroup",
    }


def peer(namespace, labels):
    return {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": namespace}
        },
        "podSelector": {"matchLabels": labels},
    }


def policy(value, namespace, spec):
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": meta(value, namespace),
        "spec": spec,
    }


def client_policy(role, ports=None):
    rules = [
        {
            "to": [peer("kube-system", {"k8s-app": "kube-dns"})],
            "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")],
        }
    ]
    if ports is not None:
        rules.append(
            {
                "to": [peer(SYSTEM, proxy_labels(role))],
                "ports": [{"protocol": "TCP", "port": port} for port in ports],
            }
        )
    return policy(
        "target-api-client",
        control(role),
        {
            "podSelector": {"matchLabels": {CLIENT_LABEL: role}},
            "policyTypes": ["Egress"],
            "egress": rules,
        },
    )


def proxy_policy(role, ports=None):
    rules = [
        {
            "from": [peer(SYSTEM, {"app": "operator"})],
            "ports": [{"protocol": "TCP", "port": 9002}],
        }
    ]
    if ports is not None:
        rules.append(
            {
                "from": [peer(control(role), {CLIENT_LABEL: role})],
                "ports": [{"protocol": "TCP", "port": port} for port in ports],
            }
        )
    return policy(
        role + "-proxy-ingress",
        SYSTEM,
        {
            "podSelector": {"matchLabels": proxy_labels(role)},
            "policyTypes": ["Ingress"],
            "ingress": rules,
        },
    )


def platform(api_hostname, credential_secret):
    hostname(api_hostname)
    name(credential_secret)
    items = [
        {
            "apiVersion": "tailscale.com/v1alpha1",
            "kind": "Tailnet",
            "metadata": meta(TAILNET),
            "spec": {"credentialSecret": credential_secret},
        },
        {
            "apiVersion": "tailscale.com/v1alpha1",
            "kind": "ProxyClass",
            "metadata": meta(PROXY_CLASS),
            "spec": {
                "statefulSet": {
                    "pod": {
                        "tailscaleContainer": {
                            "image": PROXY_IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                        }
                    }
                }
            },
        },
    ]
    for role in ROLES:
        ns = control(role)
        namespace_meta = meta(ns)
        namespace_meta["labels"].update(
            {
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "v1.35",
            }
        )
        items.extend(
            [
                {"apiVersion": "v1", "kind": "Namespace", "metadata": namespace_meta},
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": meta(role + "-host", ns),
                    "automountServiceAccountToken": False,
                },
                {
                    "apiVersion": "tailscale.com/v1alpha1",
                    "kind": "ProxyGroup",
                    "metadata": meta(group(role)),
                    "spec": {
                        "type": "egress",
                        "replicas": 1,
                        "tailnet": TAILNET,
                        "proxyClass": PROXY_CLASS,
                        "tags": ["tag:bravo-" + role],
                    },
                },
                {
                    "apiVersion": "tailscale.com/v1alpha1",
                    "kind": "ProxyGroupPolicy",
                    "metadata": meta("product-access", ns),
                    "spec": {"egress": [group(role)]},
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {
                        **meta("target-api", ns),
                        "annotations": {
                            "tailscale.com/tailnet-fqdn": api_hostname,
                            "tailscale.com/proxy-group": group(role),
                        },
                    },
                    "spec": {
                        "type": "ExternalName",
                        "externalName": "placeholder",
                        "ports": [
                            {
                                "name": "https",
                                "port": 443,
                                "protocol": "TCP",
                                "targetPort": 443,
                            }
                        ],
                    },
                },
                policy(
                    "control-default-deny",
                    ns,
                    {
                        "podSelector": {},
                        "policyTypes": ["Ingress", "Egress"],
                        "ingress": [],
                        "egress": [],
                    },
                ),
                client_policy(role),
                proxy_policy(role),
            ]
        )
    return items


def mapped_ports(observed, role):
    """Bind to a single operator-managed Service; do not open the allocation range."""
    matches = []
    for item in observed.get("items", []):
        md = item.get("metadata", {})
        labels = md.get("labels", {})
        expected = {
            "tailscale.com/managed": "true",
            "tailscale.com/parent-resource": "target-api",
            "tailscale.com/parent-resource-ns": control(role),
            "tailscale.com/parent-resource-type": "svc",
            "tailscale.com/proxy-group": group(role),
            "tailscale.com/svc-type": "egress",
        }
        if labels.get("tailscale.com/parent-resource-ns") != control(role):
            continue
        if any(labels.get(k) != v for k, v in expected.items()):
            raise ValueError("foreign Service labels")
        if (
            item.get("kind") != "Service"
            or md.get("namespace") != SYSTEM
            or not isinstance(md.get("uid"), str)
            or not re.fullmatch(
                r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", md["uid"]
            )
            or not isinstance(md.get("resourceVersion"), str)
            or not md["resourceVersion"].isdigit()
        ):
            raise ValueError("incomplete Service identity")
        ports = item.get("spec", {}).get("ports", [])
        data = [port for port in ports if port.get("name") != "tailscale-health-check"]
        if (
            len(data) != 1
            or data[0].get("port") != 443
            or data[0].get("protocol") != "TCP"
        ):
            raise ValueError("unexpected exposed port")
        target = data[0].get("targetPort")
        if type(target) is not int or not 10000 <= target < 11000:
            raise ValueError("unexpected mapped port")
        matches.append([443, target])
    if len(matches) != 1:
        raise ValueError("exactly one observed Service per role required")
    return matches[0]


def network(observed):
    # Validate both sides before producing any output.
    ports = {role: mapped_ports(observed, role) for role in ROLES}
    return [
        obj
        for role in ROLES
        for obj in (client_policy(role, ports[role]), proxy_policy(role, ports[role]))
    ]


def target_apply(namespace, deployment):
    name(namespace)
    name(deployment)
    if namespace in {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        SYSTEM,
        "bravo-apply-access",
    }:
        raise ValueError("application namespace required")
    identity_ns = "bravo-apply-access"
    identity_meta = meta(identity_ns)
    identity_meta["labels"].update(
        {
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "v1.35",
        }
    )
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": identity_meta},
        policy(
            "default-deny",
            identity_ns,
            {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        ),
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": meta("apply-deployer", identity_ns),
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": meta("bravo-apply-demo-chat", namespace),
            "rules": [
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments"],
                    "resourceNames": [deployment],
                    "verbs": ["get", "watch", "patch"],
                },
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": meta("bravo-apply-demo-chat", namespace),
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "bravo-apply-demo-chat",
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "apply-deployer",
                    "namespace": identity_ns,
                }
            ],
        },
    ]


def kubeconfig(role, api_hostname, token_file):
    control(role)
    hostname(api_hostname)
    path = PurePosixPath(token_file)
    if not path.is_absolute() or ".." in path.parts or str(path) == "/":
        raise ValueError("absolute mounted token file required")
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "target",
        "clusters": [
            {
                "name": "target",
                "cluster": {
                    "server": "https://target-api."
                    + control(role)
                    + ".svc.cluster.local",
                    "tls-server-name": api_hostname,
                },
            }
        ],
        "users": [{"name": role, "user": {"tokenFile": token_file}}],
        "contexts": [
            {"name": "target", "context": {"cluster": "target", "user": role}}
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("platform")
    p.add_argument("--api-hostname", required=True)
    p.add_argument("--credential-secret", required=True)
    sub.add_parser("network")
    p = sub.add_parser("target-apply")
    p.add_argument("--namespace", required=True)
    p.add_argument("--deployment", required=True)
    p = sub.add_parser("kubeconfig")
    p.add_argument("--role", choices=ROLES, required=True)
    p.add_argument("--api-hostname", required=True)
    p.add_argument("--token-file", required=True)
    args = parser.parse_args(argv)
    if args.command == "platform":
        result = platform(args.api_hostname, args.credential_secret)
    elif args.command == "network":
        result = network(json.load(sys.stdin))
    elif args.command == "target-apply":
        result = target_apply(args.namespace, args.deployment)
    else:
        result = kubeconfig(args.role, args.api_hostname, args.token_file)
    print(
        json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": result}
            if isinstance(result, list)
            else result,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
