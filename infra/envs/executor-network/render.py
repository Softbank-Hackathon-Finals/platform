#!/usr/bin/env python3
"""Add only the dedicated T12 caller's platform-state DB network path."""
import json

CALLER = {"app.kubernetes.io/name": "cutover-executor"}
DATABASE = {"cnpg.io/cluster": "platform-db"}


def peer(namespace, labels):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": labels}}


def policy(name, namespace, spec):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"component": "platform", "app.kubernetes.io/part-of": "bravo-cutover-network"}},
            "spec": spec}


def state_database():
    return [
        policy("cutover-executor-database", "platform-state", {
            "podSelector": {"matchLabels": DATABASE}, "policyTypes": ["Ingress"],
            "ingress": [{"from": [peer("bravo-platform", CALLER)],
                         "ports": [{"protocol": "TCP", "port": 5432}]}],
        }),
        policy("cutover-executor-state", "bravo-platform", {
            "podSelector": {"matchLabels": CALLER}, "policyTypes": ["Egress"],
            "egress": [{"to": [peer("platform-state", DATABASE)],
                        "ports": [{"protocol": "TCP", "port": 5432}]},
                       {"to": [peer("kube-system", {"k8s-app": "kube-dns"})],
                        "ports": [{"protocol": "TCP", "port": 53}, {"protocol": "UDP", "port": 53}]}],
        }),
    ]


if __name__ == "__main__":
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": state_database()}, indent=2))
