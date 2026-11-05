"""Render portable personal access bundles; never issue credentials or apply resources."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import UUID

VERSION = "bravo.dev/personal-access/v1"
RBAC = "rbac.authorization.k8s.io"
READ = ["get", "list", "watch"]
WRITE = READ + ["create", "update", "patch", "delete"]
# This is a security ceiling, not an environment inventory. Unknown APIs fail closed.
RESOURCES = {
    "": {
        "pods",
        "services",
        "configmaps",
        "secrets",
        "persistentvolumeclaims",
        "serviceaccounts",
        "resourcequotas",
        "limitranges",
        "events",
    },
    "apps": {"deployments", "statefulsets", "replicasets"},
    "batch": {"jobs"},
    "networking.k8s.io": {"networkpolicies"},
    RBAC: {"roles", "rolebindings"},
}


def fields(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise ValueError("missing or unsupported profile fields")
    return value


def text(value):
    if (
        not isinstance(value, str)
        or not value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("nonempty text without control characters required")
    return value


def name(value):
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", text(value)):
        raise ValueError("DNS label required")
    return value


def uid(value):
    if str(UUID(text(value))) != value:
        raise ValueError("canonical resource UID required")
    return value


def host(value):
    text(value)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if len(value) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[-A-Za-z0-9]{0,61}[A-Za-z0-9])?", p)
            for p in value.split(".")
        ):
            raise ValueError("DNS hostname or IP required")
    return value


def absolute_path(value):
    text(value)
    # kubeconfig file references are interpreted on the recipient's device, not here.
    if not (
        value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)
    ) or ".." in re.split(r"[/\\]", value):
        raise ValueError("absolute recipient-device file path required")
    return value


def rules(values, readonly=False):
    if not isinstance(values, list) or not values:
        raise ValueError("explicit resource rules required")
    out = []
    for entry in values:
        fields(entry, ("apiGroup", "resources", "verbs"))
        group = entry["apiGroup"]
        permitted = RESOURCES.get(group, set())
        resources, verbs = entry["resources"], entry["verbs"]
        if (
            not isinstance(resources, list)
            or not resources
            or any(not isinstance(r, str) or r not in permitted for r in resources)
        ):
            raise ValueError("resource outside personal namespace security ceiling")
        if (
            not isinstance(verbs, list)
            or not verbs
            or any(v not in (READ if readonly else WRITE) for v in verbs)
        ):
            raise ValueError(
                "unsupported verb (no wildcard, bind, escalate or deletecollection)"
            )
        if readonly and any(r not in {"pods", "networkpolicies"} for r in resources):
            raise ValueError(
                "external namespace reads are limited to pods and networkpolicies"
            )
        out.append(
            {
                "apiGroups": [group],
                "resources": sorted(set(resources)),
                "verbs": sorted(set(verbs)),
            }
        )
    return out


def validate(p):
    fields(
        p,
        (
            "apiVersion",
            "kind",
            "name",
            "cluster",
            "auth",
            "username",
            "namespace",
            "permissions",
        ),
        ("testRunner",),
    )
    if p["apiVersion"] != VERSION or p["kind"] != "PersonalAccessProfile":
        raise ValueError("unsupported profile version or kind")
    name(p["name"])
    c = fields(
        p["cluster"], ("server", "uid", "trust"), ("tlsServerName", "inClusterServer")
    )
    server = urlsplit(text(c["server"]))
    if (
        server.scheme != "https"
        or not server.hostname
        or server.username
        or server.password
        or server.path not in ("", "/")
        or server.query
        or server.fragment
        or server.port == 0
    ):
        raise ValueError("HTTPS API origin without credentials/path/query required")
    host(server.hostname)
    if "inClusterServer" in c:
        internal = urlsplit(text(c["inClusterServer"]))
        if (
            internal.scheme != "https"
            or not internal.hostname
            or internal.username
            or internal.password
            or internal.path not in ("", "/")
            or internal.query
            or internal.fragment
            or internal.port == 0
        ):
            raise ValueError("in-cluster HTTPS API origin required")
        host(internal.hostname)
    uid(c["uid"])
    trust = fields(c["trust"], ("mode",), ("caFile",))
    if trust["mode"] == "system":
        fields(trust, ("mode",))
    elif trust["mode"] == "ca-file":
        fields(trust, ("mode", "caFile"))
        absolute_path(trust["caFile"])
    else:
        raise ValueError(
            "TLS trust must be system or ca-file; verification cannot be disabled"
        )
    if "tlsServerName" in c:
        host(c["tlsServerName"])
    username = text(p["username"])
    if username.startswith("system:") or "*" in username or len(username) > 253:
        raise ValueError("individual human username required")
    n = fields(p["namespace"], ("name", "uid"))
    name(n["name"])
    uid(n["uid"])
    if n["name"] == "default" or n["name"].startswith("kube-"):
        raise ValueError("dedicated pre-provisioned namespace required")
    if "testRunner" in p:
        runner = fields(
            p["testRunner"],
            (
                "serviceAccount",
                "impersonateServiceAccount",
                "namespace",
                "podName",
                "apiEndpoints",
            ),
        )
        name(runner["serviceAccount"])
        name(runner["impersonateServiceAccount"])
        name(runner["podName"])
        fields(runner["namespace"], ("name", "uid"))
        name(runner["namespace"]["name"])
        uid(runner["namespace"]["uid"])
        if not isinstance(runner["apiEndpoints"], list) or not runner["apiEndpoints"]:
            raise ValueError("observed Kubernetes API endpoints required")
        for endpoint in runner["apiEndpoints"]:
            fields(endpoint, ("ip", "port"))
            ipaddress.ip_address(text(endpoint["ip"]))
            if type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65535:
                raise ValueError("API endpoint TCP port required")
        if runner["namespace"]["name"] in {n["name"], "default"} or runner["namespace"][
            "name"
        ].startswith("kube-"):
            raise ValueError("test manager must be in a separate dedicated namespace")
        if runner["serviceAccount"] == runner["impersonateServiceAccount"]:
            raise ValueError("test manager and tested service account must be separate")
    a = p["auth"]
    if not isinstance(a, dict):
        raise ValueError("auth object required")
    mode = a.get("mode")
    if mode == "tailscale-proxy":
        fields(a, ("mode",))
    elif mode == "client-certificate":
        fields(a, ("mode", "certificateFile", "keyFile"))
        absolute_path(a["certificateFile"])
        absolute_path(a["keyFile"])
    elif mode == "exec":
        fields(a, ("mode", "command", "args", "interactiveMode"))
        absolute_path(a["command"])
        if not isinstance(a["args"], list) or any(
            not isinstance(v, str) or any(ord(c) < 32 for c in v) for v in a["args"]
        ):
            raise ValueError("exec args must be a string array")
        if a["interactiveMode"] not in {"Never", "IfAvailable", "Always"}:
            raise ValueError("invalid exec interactiveMode")
    else:
        raise ValueError("unsupported authentication mode")
    permissions = fields(
        p["permissions"], ("namespaceRules", "podLogs", "podExec", "readOnlyNamespaces")
    )
    rules(permissions["namespaceRules"])
    if any(type(permissions[k]) is not bool for k in ("podLogs", "podExec")):
        raise ValueError("podLogs and podExec must be boolean")
    if not isinstance(permissions["readOnlyNamespaces"], list):
        raise ValueError("readOnlyNamespaces must be an array")
    seen = {n["name"]} | (
        {p["testRunner"]["namespace"]["name"]} if "testRunner" in p else set()
    )
    for entry in permissions["readOnlyNamespaces"]:
        fields(entry, ("name", "rules"))
        name(entry["name"])
        if entry["name"] in seen:
            raise ValueError("duplicate or writable external namespace")
        seen.add(entry["name"])
        rules(entry["rules"], readonly=True)
    return p


def kubeconfig(p):
    validate(p)
    c, a, key = p["cluster"], p["auth"], p["name"]
    cluster = {"server": c["server"]}
    if c["trust"]["mode"] == "ca-file":
        cluster["certificate-authority"] = c["trust"]["caFile"]
    if "tlsServerName" in c:
        cluster["tls-server-name"] = c["tlsServerName"]
    user = {}
    if a["mode"] == "client-certificate":
        user = {"client-certificate": a["certificateFile"], "client-key": a["keyFile"]}
    elif a["mode"] == "exec":
        user = {
            "exec": {
                "apiVersion": "client.authentication.k8s.io/v1",
                "command": a["command"],
                "args": a["args"],
                "interactiveMode": a["interactiveMode"],
            }
        }
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": key, "cluster": cluster}],
        "users": [{"name": key, "user": user}],
        "contexts": [
            {
                "name": key,
                "context": {
                    "cluster": key,
                    "user": key,
                    "namespace": p["namespace"]["name"],
                },
            }
        ],
        "current-context": key,
    }


def rbac(p):
    validate(p)
    key, ns = p["name"], p["namespace"]["name"]
    labels = {
        "app.kubernetes.io/managed-by": "bravo-personal-access",
        "bravo.dev/access-profile": key,
    }
    subject = [{"kind": "User", "apiGroup": RBAC, "name": p["username"]}]
    if "testRunner" in p:
        subject.append(
            {
                "kind": "ServiceAccount",
                "name": p["testRunner"]["serviceAccount"],
                "namespace": p["testRunner"]["namespace"]["name"],
            }
        )

    def pair(kind, namespace, role_rules):
        metadata = {"name": key, "labels": labels.copy()}
        if namespace:
            metadata["namespace"] = namespace
        return [
            {
                "apiVersion": RBAC + "/v1",
                "kind": kind,
                "metadata": metadata,
                "rules": role_rules,
            },
            {
                "apiVersion": RBAC + "/v1",
                "kind": kind + "Binding",
                "metadata": metadata.copy(),
                "subjects": subject,
                "roleRef": {"apiGroup": RBAC, "kind": kind, "name": key},
            },
        ]

    own = rules(p["permissions"]["namespaceRules"])
    if "testRunner" in p:
        # ServiceAccount impersonation is namespaced in the API authorizer.
        # Do not use a ClusterRole: it would allow the same SA name in every namespace.
        own.append(
            {
                "apiGroups": [""],
                "resources": ["serviceaccounts"],
                "resourceNames": [p["testRunner"]["impersonateServiceAccount"]],
                "verbs": ["impersonate"],
            }
        )
    for enabled, resource, verbs in [
        ("podLogs", "pods/log", ["get"]),
        ("podExec", "pods/exec", ["get", "create"]),
    ]:
        if p["permissions"][enabled]:
            own.append({"apiGroups": [""], "resources": [resource], "verbs": verbs})
    items = pair("Role", ns, own)
    if "testRunner" in p:
        items.insert(
            0,
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": p["testRunner"]["serviceAccount"],
                    "namespace": p["testRunner"]["namespace"]["name"],
                    "labels": labels.copy(),
                },
                "automountServiceAccountToken": False,
            },
        )
        manager = p["testRunner"]
        manager_access = pair(
            "Role",
            manager["namespace"]["name"],
            [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "pods/log"],
                    "resourceNames": [manager["podName"]],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods/exec"],
                    "resourceNames": [manager["podName"]],
                    "verbs": ["get", "create"],
                },
            ],
        )
        manager_access[1]["subjects"] = subject[:1]
        items += manager_access
    for entry in p["permissions"]["readOnlyNamespaces"]:
        items += pair("Role", entry["name"], rules(entry["rules"], readonly=True))
    items += pair(
        "ClusterRole",
        None,
        [
            {
                "apiGroups": [""],
                "resources": ["namespaces"],
                "resourceNames": ["kube-system", ns]
                + ([p["testRunner"]["namespace"]["name"]] if "testRunner" in p else []),
                "verbs": ["get"],
            }
        ],
    )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def verify(p, cluster, namespace, whoami=None, runner_namespace=None):
    validate(p)
    identities = [
        (cluster, "kube-system", p["cluster"]["uid"]),
        (namespace, p["namespace"]["name"], p["namespace"]["uid"]),
    ]
    if "testRunner" in p:
        if runner_namespace is None:
            raise ValueError("test manager namespace readback required")
        identities.append(
            (
                runner_namespace,
                p["testRunner"]["namespace"]["name"],
                p["testRunner"]["namespace"]["uid"],
            )
        )
    for obj, expected_name, expected_uid in identities:
        meta = obj.get("metadata", {})
        if (
            obj.get("kind") != "Namespace"
            or meta.get("name") != expected_name
            or meta.get("uid") != expected_uid
            or meta.get("deletionTimestamp")
        ):
            raise ValueError("cluster/namespace identity changed or is terminating")
        if (
            expected_name != "kube-system"
            and meta.get("labels", {}).get("pod-security.kubernetes.io/enforce")
            != "restricted"
        ):
            raise ValueError("namespace must enforce restricted Pod Security")
    if whoami is not None:
        info = whoami.get("status", {}).get("userInfo", {})
        if (
            whoami.get("kind") != "SelfSubjectReview"
            or info.get("username") != p["username"]
            or "system:masters" in info.get("groups", [])
        ):
            raise ValueError(
                "actual personal login identity mismatch or administrator group"
            )
    return {
        "clusterIdentityVerified": True,
        "namespaceIdentityVerified": True,
        "personalLoginVerified": whoami is not None,
    }


def network(p):
    validate(p)
    if "testRunner" not in p:
        raise ValueError("testRunner required for management network policy")
    runner = p["testRunner"]
    labels = {"bravo.dev/access-profile": p["name"]}
    metadata = {"namespace": runner["namespace"]["name"], "labels": labels}
    policies = []
    for suffix, spec in [
        (
            "deny",
            {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        ),
        (
            "api",
            {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {"ipBlock": {"cidr": str(ipaddress.ip_network(e["ip"]))}}
                        ],
                        "ports": [{"protocol": "TCP", "port": e["port"]}],
                    }
                    for e in runner["apiEndpoints"]
                ],
            },
        ),
    ]:
        policies.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {**metadata, "name": p["name"] + "-" + suffix},
                "spec": spec,
            }
        )
    return {"apiVersion": "v1", "kind": "List", "items": policies}


def probe(p, image, service_account, pod_name):
    validate(p)
    if "inClusterServer" not in p["cluster"]:
        raise ValueError("probe requires verified cluster.inClusterServer")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("reviewed digest-pinned kubectl image required")
    name(service_account)
    name(pod_name)
    namespace_name = p["namespace"]["name"]
    if "testRunner" in p and service_account == p["testRunner"]["serviceAccount"]:
        if pod_name != p["testRunner"]["podName"]:
            raise ValueError("manager Pod name must match its restricted access Role")
        namespace_name = p["testRunner"]["namespace"]["name"]
    mounted = "/var/run/secrets/kubernetes.io/serviceaccount/"
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "internal",
                "cluster": {
                    "server": p["cluster"]["inClusterServer"],
                    "certificate-authority": mounted + "ca.crt",
                },
            }
        ],
        "users": [{"name": "pod", "user": {"tokenFile": mounted + "token"}}],
        "contexts": [
            {
                "name": "internal",
                "context": {
                    "cluster": "internal",
                    "user": "pod",
                    "namespace": p["namespace"]["name"],
                },
            }
        ],
        "current-context": "internal",
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace_name,
            "labels": {"bravo.dev/access-profile": p["name"]},
            "annotations": {"bravo.dev/in-cluster-config": json.dumps(config)},
        },
        "spec": {
            "serviceAccountName": service_account,
            "automountServiceAccountToken": True,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 1800,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumes": [
                {
                    "name": "kubeconfig",
                    "downwardAPI": {
                        "items": [
                            {
                                "path": "config",
                                "fieldRef": {
                                    "fieldPath": "metadata.annotations['bravo.dev/in-cluster-config']"
                                },
                            }
                        ]
                    },
                },
                {"name": "workspace", "emptyDir": {"sizeLimit": "128Mi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}},
            ],
            "containers": [
                {
                    "name": "kubectl",
                    "image": image,
                    "command": ["sleep", "1800"],
                    "workingDir": "/work",
                    "env": [
                        {"name": "KUBECONFIG", "value": "/etc/bravo-kubeconfig/config"}
                    ],
                    "volumeMounts": [
                        {
                            "name": "kubeconfig",
                            "mountPath": "/etc/bravo-kubeconfig",
                            "readOnly": True,
                        },
                        {"name": "workspace", "mountPath": "/work"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "128Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                    },
                }
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["kubeconfig", "rbac", "verify", "probe", "network"]
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--cluster-readback", type=Path)
    parser.add_argument("--namespace-readback", type=Path)
    parser.add_argument("--whoami-readback", type=Path)
    parser.add_argument("--runner-namespace-readback", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--service-account")
    parser.add_argument("--pod-name")
    args = parser.parse_args()
    try:
        p = validate(json.loads(args.profile.read_text()))
        if args.action == "kubeconfig":
            result = kubeconfig(p)
        elif args.action == "rbac":
            result = rbac(p)
        elif args.action == "network":
            result = network(p)
        elif args.action == "probe":
            if not all([args.image, args.service_account, args.pod_name]):
                raise ValueError("probe requires image, service-account and pod-name")
            result = probe(p, args.image, args.service_account, args.pod_name)
        else:
            if not args.cluster_readback or not args.namespace_readback:
                raise ValueError("verify requires both cluster and namespace readbacks")
            result = verify(
                p,
                json.loads(args.cluster_readback.read_text()),
                json.loads(args.namespace_readback.read_text()),
                json.loads(args.whoami_readback.read_text())
                if args.whoami_readback
                else None,
                json.loads(args.runner_namespace_readback.read_text())
                if args.runner_namespace_readback
                else None,
            )
        print(json.dumps(result, indent=2))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        # Do not echo private input, credential paths, usernames or subprocess output.
        print(
            f"personal-access: {type(exc).__name__}; invalid input or unreadable file",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
