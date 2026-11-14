#!/usr/bin/env python3
"""Render the common base of a managed cluster (GKE, EKS) for either role; never contact a cluster.

A10 (ADR-0034 3.2 rule 3): every environment has Kubernetes + CNPG + PgBouncer + Tailscale operator +
cloudflared. The on-prem renderers (target-workloads, target-secrets) stay the source of the database
and ESO shapes; this file only adapts them to a side (`source` · `target`) and to cloud storage and
cloud secret stores. It never emits a credential value: the secret store authenticates with the ESO
controller's workload identity (GKE Workload Identity, EKS Pod Identity), set up by the cluster roots.
"""
import argparse
import importlib.util
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SIDES = ("source", "target")
STORAGE = {"gke": "standard-rwo", "eks": "bravo-gp3"}  # GKE's default PD class; EKS gets a gp3 class below
STORE_NAME = "bravo-cloud-secrets"


def _load(relative, name):
    spec = importlib.util.spec_from_file_location(name, HERE.parent / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workloads = _load("target-workloads/render.py", "target_workloads_render")
secrets = _load("target-secrets/render.py", "target_secrets_render")


def _side(side):
    if side not in SIDES:
        raise ValueError("source or target required")
    return side


def _relabel(item, side):
    labels = item.setdefault("metadata", {}).setdefault("labels", {})
    labels.update({"component": side, "app.kubernetes.io/part-of": f"bravo-{side}"})
    return item


def _for_side(items, side):
    """The target renderers name everything `target-*`; a source-side cluster gets `source-*`."""
    text = json.dumps(items)
    if side != "target":
        text = text.replace('"target-', f'"{side}-').replace(".target-dev", f".{side}-dev")
    return [_relabel(item, side) for item in json.loads(text)]


def namespaces(side):
    """The application namespace (restricted) and its token-less default account, plus ESO's namespace."""
    _side(side)
    namespace = next(i for i in workloads.database("placeholder") if i["kind"] == "Namespace")
    default = workloads.resource("v1", "ServiceAccount", "default", automountServiceAccountToken=False)
    return _for_side([namespace, default], side) + [_relabel(secrets.eso_namespace(), side)]


def database(side, storage):
    """CNPG Cluster on the cloud's dynamic volumes: the on-prem local PV and StorageClass are dropped."""
    _side(side)
    if storage not in STORAGE:
        raise ValueError("gke or eks required")
    items = [i for i in workloads.database("placeholder")
             if i["kind"] not in ("Namespace", "PersistentVolume", "StorageClass")]
    for item in items:
        if item["kind"] == "Cluster":
            item["spec"]["storage"] = {"size": "8Gi", "storageClass": STORAGE[storage]}
    extra = []
    if storage == "eks":
        extra.append({
            "apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": STORAGE["eks"], "labels": {"component": side}},
            "provisioner": "ebs.csi.aws.com", "reclaimPolicy": "Retain",
            "volumeBindingMode": "WaitForFirstConsumer", "allowVolumeExpansion": True,
            "parameters": {"type": "gp3", "encrypted": "true"},
        })
    return extra + _for_side(items, side)


def cnpg_values(side):
    _side(side)
    values = yaml.safe_load((HERE.parent / "target-workloads/cnpg-values.yaml").read_text())
    values["config"]["data"]["WATCH_NAMESPACE"] = f"{side}-dev"
    return values


def eso_values(side):
    _side(side)
    return secrets.eso_values() | {"scopedNamespace": f"{side}-dev"}


def secret_store(side, provider, *, project_id=None, region=None):
    """A namespaced SecretStore on the cloud's secret service, authenticated as the ESO controller."""
    _side(side)
    if provider == "gcp":
        if not project_id:
            raise ValueError("--project-id required")
        spec = {"provider": {"gcpsm": {"projectID": project_id}}}
    elif provider == "aws":
        if not region:
            raise ValueError("--region required")
        spec = {"provider": {"aws": {"service": "SecretsManager", "region": region}}}
    else:
        raise ValueError("gcp or aws required")
    store = {"apiVersion": "external-secrets.io/v1", "kind": "SecretStore",
             "metadata": {"name": STORE_NAME, "namespace": f"{side}-dev", "labels": {}}, "spec": spec}
    return [_relabel(store, side)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["namespaces", "database", "cnpg-values", "eso-values", "secret-store"])
    parser.add_argument("--side", required=True, choices=SIDES)
    parser.add_argument("--storage", choices=sorted(STORAGE))
    parser.add_argument("--provider", choices=["gcp", "aws"])
    parser.add_argument("--project-id")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    if args.stage in ("cnpg-values", "eso-values"):
        print(json.dumps({"cnpg-values": cnpg_values, "eso-values": eso_values}[args.stage](args.side), indent=2))
        return
    items = {"namespaces": lambda: namespaces(args.side),
             "database": lambda: database(args.side, args.storage),
             "secret-store": lambda: secret_store(args.side, args.provider, project_id=args.project_id,
                                                  region=args.region)}[args.stage]()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
