#!/usr/bin/env python3
"""Add only the dedicated T12 caller's platform-state DB network path."""
import argparse
import json

NAME = "cutover-executor"
DATABASE = {"cnpg.io/cluster": "platform-db"}


def peer(namespace, labels):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": labels}}


def policy(name, namespace, spec):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"component": "platform", "app.kubernetes.io/part-of": "bravo-cutover-network"}},
            "spec": spec}


def state_database(pair=""):
    """`pair` is the deploy.py suffix of a further Executor; empty renders the existing policies."""
    name = f"{NAME}-{pair}" if pair else NAME
    caller = {"app.kubernetes.io/name": name}
    return [
        policy(name + "-database", "platform-state", {
            "podSelector": {"matchLabels": DATABASE}, "policyTypes": ["Ingress"],
            "ingress": [{"from": [peer("bravo-platform", caller)],
                         "ports": [{"protocol": "TCP", "port": 5432}]}],
        }),
        policy(name + "-state", "bravo-platform", {
            "podSelector": {"matchLabels": caller}, "policyTypes": ["Egress"],
            "egress": [{"to": [peer("platform-state", DATABASE)],
                        "ports": [{"protocol": "TCP", "port": 5432}]},
                       {"to": [peer("kube-system", {"k8s-app": "kube-dns"})],
                        "ports": [{"protocol": "TCP", "port": 53}, {"protocol": "UDP", "port": 53}]}],
        }),
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="")
    items = state_database(parser.parse_args().pair)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))
