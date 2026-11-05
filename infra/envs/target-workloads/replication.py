#!/usr/bin/env python3
"""Render bounded T8 subscription preparation. This module never executes SQL/API calls."""
import argparse
import copy
import ipaddress
import json
import sys

from render import POSTGRES_IMAGE


def names(side):
    if side not in {"source", "target"}:
        raise ValueError("exact source or target side required")
    peer = "target" if side == "source" else "source"
    return peer, f"t8-{peer}", f"t8_{peer}_to_{side}"


def external_cluster(side, hostaddr):
    peer, external, _ = names(side)
    address = ipaddress.ip_address(hostaddr)
    if address.version != 4 or not address.is_private or any([
        address.is_loopback, address.is_link_local, address.is_multicast, address.is_unspecified,
    ]):
        raise ValueError("verified private egress Service IPv4 address required")
    return {
        "name": external,
        "connectionParameters": {
            "host": f"{peer}-db-rw.{peer}-dev.svc", "hostaddr": str(address),
            "port": "5432", "dbname": "chat", "user": "chat_replicator",
            "sslmode": "verify-full", "connect_timeout": "10",
            "application_name": f"bravo_t8_{side}_subscriber",
        },
        "password": {"name": f"t8-{peer}-replication", "key": "password"},
        "sslRootCert": {"name": f"t8-{peer}-ca", "key": "ca.crt"},
    }


def external_patch(side, hostaddr, cluster):
    """Append one external reference and cap retained WAL without replacing other config."""
    external = external_cluster(side, hostaddr)
    metadata = cluster.get("metadata", {})
    spec = cluster.get("spec", {})
    if (cluster.get("kind") != "Cluster" or metadata.get("name") != f"{side}-db"
            or metadata.get("namespace") != f"{side}-dev" or not metadata.get("resourceVersion")
            or spec.get("imageName") != POSTGRES_IMAGE):
        raise ValueError("unexpected database identity, version or missing concurrency token")
    if side == "target":
        roles = [r for r in spec.get("managed", {}).get("roles", []) if r.get("name") == "chat_app"]
        if len(roles) != 1 or roles[0].get("login") is not False:
            raise ValueError("target application must remain declaratively NOLOGIN")
    prior = copy.deepcopy(spec.get("externalClusters", []))
    if not isinstance(prior, list) or any(x.get("name") == external["name"] for x in prior):
        raise ValueError("existing external reference requires owner reconciliation")
    parameters = copy.deepcopy(spec["postgresql"]["parameters"])
    # A disabled reverse slot must not retain WAL without a limit indefinitely.
    # Crossing this limit can invalidate a slot; it requires explicit resync, not data skipping.
    prior_cap = parameters.get("max_slot_wal_keep_size")
    if prior_cap is None or prior_cap.strip() == "-1":
        parameters["max_slot_wal_keep_size"] = "1GB"
    return [
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "add", "path": "/spec/externalClusters", "value": [*prior, external]},
        {"op": "add", "path": "/spec/postgresql/parameters", "value": parameters},
    ]


def subscription(side, disable_on_error=True):
    """side is where the subscription lives: "target" is the forward direction, "source" the reverse."""
    if type(disable_on_error) is not bool:
        raise ValueError("explicit Boolean disable_on_error required")
    peer, external, name = names(side)
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Subscription",
        "metadata": {"name": name.replace("_", "-"), "namespace": f"{side}-dev",
                     "labels": {"component": side, "app.kubernetes.io/part-of": "bravo-t8-replication"}},
        "spec": {
            "cluster": {"name": f"{side}-db"}, "dbname": "chat", "name": name,
            "externalClusterName": external, "publicationName": f"{peer}_chat",
            "subscriptionReclaimPolicy": "retain",
            "parameters": {
                "enabled": "true" if side == "target" else "false",
                "copy_data": "true" if side == "target" else "false",
                "create_slot": "true", "slot_name": name, "origin": "none",
                "binary": "false", "streaming": "on", "disable_on_error": "true" if disable_on_error else "false",
                "password_required": "true", "run_as_owner": "false",
            },
        },
    }


def runtime_state_sql(side, enabled):
    """Only fixed subscription identifiers and ENABLE/DISABLE; no caller SQL fragments."""
    _, _, name = names(side)
    if type(enabled) is not bool:
        raise ValueError("explicit Boolean state required")
    return f'ALTER SUBSCRIPTION "{name}" {"ENABLE" if enabled else "DISABLE"};'


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["external-patch", "subscription"])
    parser.add_argument("--side", required=True, choices=["source", "target"])
    parser.add_argument("--hostaddr")
    parser.add_argument("--disable-on-error", choices=["true", "false"], default="true",
                        help="subscription stage: this direction only (--side target is forward, --side source is reverse)")
    args = parser.parse_args()
    result = (external_patch(args.side, args.hostaddr, json.load(sys.stdin))
              if args.stage == "external-patch" else subscription(args.side, args.disable_on_error == "true"))
    print(json.dumps(result, indent=2))
