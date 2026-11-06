"""One-shot operator bootstrap of the pinned Argo chart, using host-local credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "bravo-gitops"
RELEASE = "bravo-gitops"


def verify_edge(get, runtime, expected):
    """Bind protected inputs and explicitly adopt an absent or already owned worker."""
    if set(runtime) != {"configuration", "secrets", "journalClaim", "ports"}:
        raise ValueError("edge_protected_references_required")
    if set(expected) != {"namespace_uid", "config_uid", "journal_uid", "secret_uids",
                         "deployment_uid", "service_account_uid"}:
        raise ValueError("edge_identity_required")
    def observed(kind, name, uid):
        value = get(kind, name, "platform-state")
        if value is None or value["metadata"]["uid"] != uid:
            raise ValueError("edge_input_identity_changed")
        return value
    observed("namespace", "platform-state", expected["namespace_uid"])
    config = observed("configmap", runtime["configuration"]["existingConfigMap"], expected["config_uid"])
    digest = hashlib.sha256(json.dumps(config["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    settings = json.loads(config["data"]["edge.json"])
    if (digest != runtime["configuration"]["sha256"] or settings.get("journal") != "/journal/edge.db"
            or settings.get("token_file") != "/provider/token"):
        raise ValueError("edge_persistent_configuration_required")
    claim = observed("pvc", runtime["journalClaim"], expected["journal_uid"])
    if (claim.get("status", {}).get("phase") != "Bound"
            or claim["spec"].get("volumeMode", "Filesystem") != "Filesystem"
            or claim["spec"]["accessModes"] not in (["ReadWriteOnce"], ["ReadWriteOncePod"])):
        raise ValueError("edge_persistent_single_writer_volume_required")
    if set(expected["secret_uids"]) != set(runtime["secrets"].values()):
        raise ValueError("edge_secret_identities_required")
    for name, uid in expected["secret_uids"].items():
        observed("secret", name, uid)
    for kind, key in (("deployment", "deployment_uid"), ("serviceaccount", "service_account_uid")):
        value = get(kind, "t17-edge-control", "platform-state")
        if expected[key] is None:
            if value is not None:
                raise ValueError("edge_existing_resource_requires_adoption")
        elif value is None or value["metadata"]["uid"] != expected[key]:
            raise ValueError("edge_workload_identity_changed")
        if value is not None:
            tracking = value["metadata"].get("annotations", {}).get("argocd.argoproj.io/tracking-id", "")
            if tracking and not tracking.startswith("platform-edge-control:"):
                raise ValueError("edge_other_owner")


def verify_reader(get, runtime, expected):
    """Adopt only the observed Reader; its shared identity and data stay external."""
    if set(runtime) != {"configuration", "secrets", "catalogClaim"}:
        raise ValueError("reader_protected_references_required")
    if set(expected) != {"deployment_uid", "service_uid", "service_account_uid",
                         "config_uid", "catalog_uid", "secret_uids"}:
        raise ValueError("reader_adoption_identity_required")
    def observed(kind, name, uid):
        value = get(kind, name, "platform-state")
        if value["metadata"]["uid"] != uid:
            raise ValueError("reader_adoption_identity_mismatch")
        return value
    deployment = observed("deployment", "t10-product-reader", expected["deployment_uid"])
    service = observed("service", "t10-product-reader", expected["service_uid"])
    observed("serviceaccount", "platform-runtime", expected["service_account_uid"])
    config = observed("configmap", runtime["configuration"]["existingConfigMap"], expected["config_uid"])
    observed("pvc", runtime["catalogClaim"], expected["catalog_uid"])
    names = set(runtime["secrets"].values())
    if names != set(expected["secret_uids"]):
        raise ValueError("reader_secret_identities_required")
    for name in names:
        observed("secret", name, expected["secret_uids"][name])
    digest = hashlib.sha256(json.dumps(config["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    labels = {"app.kubernetes.io/name": "t10-product-reader", "component": "platform"}
    pod = deployment["spec"]["template"]["spec"]
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    tracking = deployment["metadata"].get("annotations", {}).get("argocd.argoproj.io/tracking-id", "")
    if (digest != runtime["configuration"]["sha256"]
            or annotations.get("bravo.dev/config-sha256") != digest
            or deployment["spec"]["selector"]["matchLabels"] != labels
            or service["spec"]["selector"] != labels
            or service["spec"]["type"] != "ClusterIP"
            or pod["serviceAccountName"] != "platform-runtime"
            or pod.get("automountServiceAccountToken") is not False
            or (tracking and not tracking.startswith("platform-product-reader:"))):
        raise ValueError("reader_adoption_boundary_mismatch")
    volumes = {v["name"]: v for v in pod["volumes"]}
    if (volumes["config"]["configMap"]["name"] != runtime["configuration"]["existingConfigMap"]
            or volumes["catalog"]["persistentVolumeClaim"] != {"claimName": runtime["catalogClaim"], "readOnly": True}
            or any(volumes[name]["secret"]["secretName"] != runtime["secrets"][key]
                   for name, key in (("credential", "database"), ("database-ca", "databaseCA"),
                                     ("catalog-key", "catalogKey"), ("tls", "tls")))):
        raise ValueError("reader_mount_change_requires_separate_review")


def run(args, *, check=True):
    result = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=240
    )
    if check and result.returncode:
        raise ValueError("bootstrap_command_failed; read Helm release before retry")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cluster-uid", required=True)
    p.add_argument("--deployment-uid", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--kubeconfig", required=True, type=Path)
    p.add_argument("--kubectl", default="kubectl")
    p.add_argument("--runtime-values", type=Path)
    p.add_argument("--reader-adoption", type=Path)
    p.add_argument("--edge-adoption", type=Path)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", a.revision):
        raise ValueError("full_revision_required")
    git = ["git", "-C", str(ROOT)]
    if (
        run(git + ["rev-parse", "HEAD"]).stdout.strip() != a.revision
        or run(git + ["status", "--porcelain"]).stdout
    ):
        raise ValueError("clean_reviewed_revision_required")
    if (
        run(["helm", "version", "--template", "{{.Version}}"]).stdout.strip()
        != "v4.0.0"
    ):
        raise ValueError("pinned_helm_v4_0_0_required")
    k = [a.kubectl, "--kubeconfig", str(a.kubeconfig), "--request-timeout=25s"]

    def get(kind, name, namespace=None):
        args = k + ["get", kind, name, "-o", "json"]
        return json.loads(run(args + (["-n", namespace] if namespace else [])).stdout)

    if get("namespace", "kube-system")["metadata"]["uid"] != a.cluster_uid:
        raise ValueError("cluster_identity_mismatch")
    deployment = get("deployment", "t10-registration", "platform-state")
    if deployment["metadata"]["uid"] != a.deployment_uid:
        raise ValueError("workload_identity_mismatch")
    helm = ["helm", "--kubeconfig", str(a.kubeconfig), "--namespace", NAMESPACE]
    releases = json.loads(
        run(
            helm + ["list", "--filter", "^" + RELEASE + "$", "-o", "json"]
        ).stdout
    )
    if releases and releases[0]["status"] != "deployed":
        raise ValueError("existing_release_not_deployed; inspect before recovery")
    values = []
    runtime_hash = None
    if a.runtime_values:
        if a.runtime_values.resolve().is_relative_to(ROOT):
            raise ValueError("runtime_input_must_remain_outside_checkout")
        value = json.loads(a.runtime_values.read_text())
        if (
            not {"registration"}.issubset(value)
            or not set(value).issubset({"registration", "productReader", "edgeControl"})
            or set(value["registration"]) != {"enabled", "runtime"}
            or value["registration"]["enabled"] is not True
        ):
            raise ValueError("registration_only_runtime_input_required")
        runtime = value["registration"]["runtime"]
        if not set(runtime).issubset({"configuration", "secrets", "hostAliases"}):
            raise ValueError("runtime_image_override_forbidden")
        # Helm validates the workload schema again during Argo rendering.
        image = json.loads(
            (
                ROOT / "infra/gitops/environments/development/registration-image.json"
            ).read_text()
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image["image"]["digest"]):
            raise ValueError("published_image_required")
        for name in set(runtime["secrets"].values()):
            get("secret", name, "platform-state")
        get(
            "configmap", runtime["configuration"]["existingConfigMap"], "platform-state"
        )
        runtime_hash = hashlib.sha256(a.runtime_values.read_bytes()).hexdigest()
        if "productReader" in value:
            reader = value["productReader"]
            if (set(reader) != {"enabled", "runtime"} or reader["enabled"] is not True
                    or a.reader_adoption is None
                    or a.reader_adoption.resolve().is_relative_to(ROOT)):
                raise ValueError("protected_reader_adoption_required")
            verify_reader(get, reader["runtime"], json.loads(a.reader_adoption.read_text()))
            reader_image = json.loads((ROOT / "infra/gitops/environments/development/product-reader-image.json").read_text())
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", reader_image["image"]["digest"]):
                raise ValueError("published_reader_image_required")
        if "edgeControl" in value:
            edge = value["edgeControl"]
            if (set(edge) != {"enabled", "runtime"} or edge["enabled"] is not True
                    or a.edge_adoption is None or a.edge_adoption.resolve().is_relative_to(ROOT)):
                raise ValueError("protected_edge_adoption_required")
            def get_optional(kind, name, namespace):
                output = run(k + ["-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json"]).stdout
                return json.loads(output) if output.strip() else None
            verify_edge(get_optional, edge["runtime"], json.loads(a.edge_adoption.read_text()))
            edge_image = json.loads((ROOT / "infra/gitops/environments/development/edge-control-image.json").read_text())
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", edge_image["image"]["digest"]):
                raise ValueError("published_edge_image_required")
        values = ["--values", str(a.runtime_values)]
    chart = ROOT / "infra/gitops/argocd"
    run(
        [
            "helm",
            "repo",
            "add",
            "bravo-argo",
            "https://argoproj.github.io/argo-helm",
            "--force-update",
        ]
    )
    run(["helm", "dependency", "build", str(chart)])
    render = run(
        [
            "helm",
            "template",
            RELEASE,
            str(chart),
            "--namespace",
            NAMESPACE,
            "--kube-version",
            "1.35.9",
            *values,
        ]
    )
    result = {
        "revision": a.revision,
        "manifest_sha256": hashlib.sha256(render.stdout.encode()).hexdigest(),
        "runtime_input_sha256": runtime_hash,
        "registration_enabled": bool(values),
        "applied": False,
    }
    if a.apply:
        # Helm owns only the bootstrap; Argo owns the registration workload.
        run(
            helm
            + [
                "upgrade",
                "--install",
                RELEASE,
                str(chart),
                "--create-namespace",
                "--wait",
                "--timeout",
                "3m",
                *values,
            ]
        )
        status = json.loads(run(helm + ["status", RELEASE, "-o", "json"]).stdout)
        result.update(
            applied=status["info"]["status"] == "deployed",
            release_revision=status["version"],
        )
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit(
            "bootstrap_failed; inspect existing release; no automatic resubmit"
        ) from None
