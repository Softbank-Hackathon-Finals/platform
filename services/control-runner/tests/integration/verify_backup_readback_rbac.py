"""Opt-in Kubernetes qualification against an existing owned backup request.

Creates only one random status-reader SA/Role/Binding. Never creates/deletes a
backup, cluster or store. Forbidden write probes use dryRun=All. Credentials stay
in memory/stdin; no tokens or raw kubeconfig are written to results.
Requires the separately reviewed CNPGBackupReader implementation (T35 PR #120).
"""

import argparse
import base64
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from bravo_controls.backup_rbac import backup_readback_rbac, restore_readback_rbac
from bravo_controls.runner import Environment, RunContext


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(kubeconfig, request, output, *, mode="backup"):
    if mode not in {"backup", "restore"}:
        raise ValueError("unknown qualification mode")
    from bravo_controls.cnpg_readback import CNPGBackupReader, RequestBinding

    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    raw = json.loads(request.read_text())
    raw["context"]["environment"] = Environment(**raw["context"]["environment"])
    raw["context"] = RunContext(**raw["context"])
    raw["requested_at"] = datetime.fromisoformat(raw["requested_at"])
    if mode == "backup":
        binding = RequestBinding(**raw)
        cluster_name, cluster_uid = binding.source_name, binding.source_uid
        store_name, store_uid = binding.store_name, binding.store_uid
        backup_name, backup_uid = binding.backup_name, binding.backup_uid
    else:
        from bravo_controls.backup_restore import CompletedBackup
        from bravo_controls.cnpg_restore_readback import RestoreRequestBinding

        b = raw["backup"]
        b["context"] = raw["context"]
        for field in ("requested_at", "started_at", "completed_at"):
            b[field] = datetime.fromisoformat(b[field])
        raw["backup"] = CompletedBackup(**b)
        binding = RestoreRequestBinding(**raw)
        cluster_name, cluster_uid = binding.target_name, binding.target_uid
        store_name, store_uid = binding.reader_store_name, binding.reader_store_uid
        backup_name, backup_uid = "not-registered", binding.backup.backup_uid
    namespace = binding.namespace
    name = "bravo-backup-read-" + uuid4().hex[:12]
    manifests = (
        backup_readback_rbac(
            namespace=namespace,
            identity=name,
            source_cluster=cluster_name,
            object_store=store_name,
            backup=backup_name,
        )
        if mode == "backup"
        else restore_readback_rbac(
            namespace=namespace,
            identity=name,
            restored_cluster=cluster_name,
            reader_store=store_name,
        )
    )
    cli = ["kubectl", "--kubeconfig", str(kubeconfig)]

    def kub(*args, data=None):
        p = subprocess.run(cli + list(args), input=data, capture_output=True, text=True, timeout=30)
        if p.returncode:
            raise RuntimeError("operator Kubernetes request failed; details withheld")
        return p.stdout

    config = json.loads(kub("config", "view", "--minify", "--raw", "-o", "json"))
    cluster = config["clusters"][0]["cluster"]
    endpoint = urllib.parse.urlsplit(cluster["server"])
    if (
        cluster.get("insecure-skip-tls-verify")
        or endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in {"", "/"}
    ):
        raise ValueError("certificate-verified API endpoint required")
    ca = base64.b64decode(cluster["certificate-authority-data"], validate=True).decode()
    tls = ssl.create_default_context(cadata=ca)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    # No proxy inheritance; a test uses the exact registered API address.
    client = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=tls), NoRedirect()
    )
    created = []
    report = {
        "scope": "real Kubernetes RBAC; restore mode validates object identity, not SQL or AWS",
        "mode": mode,
    }
    base = f"/apis/postgresql.cnpg.io/v1/namespaces/{namespace}"
    cluster_path = base + "/clusters/" + cluster_name
    backup_path = base + "/backups/" + backup_name
    store_path = f"/apis/barmancloud.cnpg.io/v1/namespaces/{namespace}/objectstores/{store_name}"

    def remove(obj):
        kind = obj["kind"]
        prefix, resource = (
            ("/api/v1", "serviceaccounts")
            if kind == "ServiceAccount"
            else (
                "/apis/rbac.authorization.k8s.io/v1",
                "roles" if kind == "Role" else "rolebindings",
            )
        )
        path = f"{prefix}/namespaces/{namespace}/{resource}/{name}"
        kub(
            "delete",
            "--raw",
            path,
            "-f",
            "-",
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": obj["metadata"]["uid"]},
                }
            ),
        )

    try:
        for manifest in manifests:
            created.append(
                json.loads(kub("create", "-f", "-", "-o", "json", data=json.dumps(manifest)))
            )
        token_request = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenRequest",
            "spec": {"expirationSeconds": 600},
        }
        # API-default audience: no arbitrary external service audience is requested.
        token = json.loads(
            kub(
                "create",
                "--raw",
                f"/api/v1/namespaces/{namespace}/serviceaccounts/{name}/token",
                "-f",
                "-",
                data=json.dumps(token_request),
            )
        )["status"]["token"]

        def api(path, method="GET", body=None, content_type="application/json"):
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = content_type
            request = urllib.request.Request(
                cluster["server"].rstrip("/") + path,
                method=method,
                headers=headers,
                data=None if body is None else json.dumps(body).encode(),
            )
            try:
                response = client.open(request, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                payload = response.read(262145)
                if len(payload) > 262144:
                    raise ValueError("bounded API read exceeded")
                return response.status, json.loads(payload)

        def get(resource, ns, resource_name):
            if ns != namespace:
                raise ValueError("unregistered namespace")
            paths = {
                ("clusters.postgresql.cnpg.io", cluster_name): cluster_path,
                ("backups.postgresql.cnpg.io", backup_name): backup_path,
                ("objectstores.barmancloud.cnpg.io", store_name): store_path,
            }
            status, document = api(paths[(resource, resource_name)])
            if status != 200:
                raise PermissionError("readback resource access denied")
            return document

        def eventually(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.2)
            raise RuntimeError("bounded RBAC propagation did not reach expected state")

        eventually(lambda: api(cluster_path)[0] == 200)
        evidence = []
        if mode == "backup":
            reader = CNPGBackupReader(
                binding=binding, get_resource=get, evidence_sink=evidence.append
            )
        else:
            from bravo_controls.cnpg_readback import resource_fingerprint

            def reader(context):
                if context != binding.context:
                    raise ValueError("wrong registered context")
                values = {}
                for kind, resource, object_name, uid, expected in (
                    (
                        "Cluster",
                        "clusters.postgresql.cnpg.io",
                        cluster_name,
                        cluster_uid,
                        binding.target_sha256,
                    ),
                    (
                        "ObjectStore",
                        "objectstores.barmancloud.cnpg.io",
                        store_name,
                        store_uid,
                        binding.reader_store_sha256,
                    ),
                ):
                    observed = resource_fingerprint(
                        get(resource, namespace, object_name),
                        kind=kind,
                        namespace=namespace,
                        name=object_name,
                        uid=uid,
                    )
                    if observed != expected:
                        raise ValueError("registered restore identity changed")
                    values[kind] = observed
                evidence.append(json.dumps(values, sort_keys=True).encode())
                return values

        receipt = reader(binding.context)
        report["named_gets_and_bound_identity_readback"] = True
        negatives = {
            "list_clusters": (base + "/clusters", "GET", None),
            "list_backups": (base + "/backups", "GET", None),
            "other_backup": (base + "/backups/not-registered", "GET", None),
            "secret": (f"/api/v1/namespaces/{namespace}/secrets/not-registered", "GET", None),
            "pods": (f"/api/v1/namespaces/{namespace}/pods", "GET", None),
            "other_namespace": ("/api/v1/namespaces/kube-system/pods", "GET", None),
            "create_backup_dry_run": (
                base + "/backups?dryRun=All",
                "POST",
                {
                    "apiVersion": "postgresql.cnpg.io/v1",
                    "kind": "Backup",
                    "metadata": {"name": name, "namespace": namespace},
                    "spec": {
                        "cluster": {"name": cluster_name},
                        "method": "plugin",
                        "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"},
                    },
                },
            ),
            "delete_backup_dry_run": (
                backup_path + "?dryRun=All",
                "DELETE",
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": backup_uid},
                },
            ),
            "patch_cluster_dry_run": (
                cluster_path + "?dryRun=All",
                "PATCH",
                {"metadata": {"annotations": {"bravo-test": "dry-run-only"}}},
            ),
        }
        if mode == "restore":
            negatives.update(
                {
                    "source_cluster": (base + "/clusters/" + binding.source_name, "GET", None),
                    "any_backup": (backup_path, "GET", None),
                    "writer_store": (
                        f"/apis/barmancloud.cnpg.io/v1/namespaces/{namespace}/objectstores/writer-store",
                        "GET",
                        None,
                    ),
                    "delete_restore_dry_run": (
                        cluster_path + "?dryRun=All",
                        "DELETE",
                        {
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "preconditions": {"uid": cluster_uid},
                        },
                    ),
                }
            )
        denied = {}
        for label, (path, method, body) in negatives.items():
            status, document = api(
                path,
                method,
                body,
                "application/merge-patch+json" if method == "PATCH" else "application/json",
            )
            denied[label] = (
                status == 403
                and document.get("kind") == "Status"
                and document.get("reason") == "Forbidden"
                and document.get("code") == 403
            )
            if not denied[label]:
                raise RuntimeError("explicit RBAC denial not established: " + label)
        report["denied"] = denied
        assert reader(binding.context) == receipt
        remove(created[-1])
        created.pop()
        eventually(lambda: api(cluster_path)[0] == 403)
        try:
            reader(binding.context)
        except PermissionError:
            report["revoked_binding_cannot_produce_receipt"] = True
        else:
            raise AssertionError("revoked status reader unexpectedly succeeded")
        created.append(
            json.loads(kub("create", "-f", "-", "-o", "json", data=json.dumps(manifests[-1])))
        )
        eventually(lambda: api(cluster_path)[0] == 200)
        assert reader(binding.context) == receipt
        report["permission_restored_positive_control"] = True
        (output / "receipt.json").write_text(
            json.dumps(
                receipt if isinstance(receipt, dict) else asdict(receipt), default=str, indent=2
            )
        )
        for i, data in enumerate(evidence):
            (output / f"evidence-{i}.json").write_bytes(data)
        report["status"] = "PASS"
    finally:
        for obj in reversed(created):
            remove(obj)
        report["own_rbac_resources_cleaned"] = True
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--mode", choices=("backup", "restore"), default="backup")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.kubeconfig.resolve(),
                args.request.resolve(),
                args.private_output.resolve(),
                mode=args.mode,
            ),
            indent=2,
        )
    )
