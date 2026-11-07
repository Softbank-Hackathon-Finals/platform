"""One-shot operator bootstrap of the pinned Argo chart, using host-local credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "bravo-gitops"
RELEASE = "bravo-gitops"


STS_HOST = "sts.ap-northeast-2.amazonaws.com"


def pinned_sts_address():
    """The single STS address the rehearsal host NetworkPolicy opens."""
    text = (ROOT / "infra/envs/rehearsal-access/rehearsal-host-network.yaml").read_text()
    pins = re.findall(r"# STS pin\n\s*- to:\n\s*- ipBlock: \{cidr: ([0-9.]+)/32\}", text)
    if len(pins) != 1:
        raise ValueError("rehearsal_host_sts_pin_unreadable")
    return pins[0]


def verify_rehearsal_host(get, host):
    """New T15 workload: every referenced input must already exist; nothing is adopted."""
    if set(host) != {"enabled", "runtime"} or host["enabled"] is not True:
        raise ValueError("protected_rehearsal_host_input_required")
    runtime = host["runtime"]
    if set(runtime) != {"configuration", "reader", "recordsClaim", "secrets", "hostAliases"}:
        raise ValueError("rehearsal_host_runtime_fields_required")
    # The host resolves STS by name, but its NetworkPolicy opens one pinned address. Without
    # the same pin in hostAliases every SSO proof is dropped (#533 review).
    sts = [a for a in runtime["hostAliases"] if STS_HOST in a.get("hostnames", [])]
    if len(sts) != 1 or sts[0].get("ip") != pinned_sts_address():
        raise ValueError("rehearsal_host_sts_pin_mismatch")
    get("configmap", runtime["configuration"]["existingConfigMap"], "platform-state")
    get("configmap", runtime["reader"]["existingConfigMap"], "platform-state")
    for claim in (runtime["reader"]["catalogClaim"], runtime["recordsClaim"]):
        get("pvc", claim, "platform-state")
    for name in set(runtime["secrets"].values()):
        get("secret", name, "platform-state")
    image = json.loads((ROOT / "infra/gitops/environments/development/rehearsal-host-image.json").read_text())
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image["image"]["digest"]):
        raise ValueError("published_rehearsal_host_image_required")


INITIAL_PROPOSAL_REGISTRY = "platform-ghcr-reader"


def verify_initial_proposal(get, value):
    """#503 Job path: namespace, SA, admission policy and the PUBLIC broker CA only.

    The Job Pod pulls the Reader image with the existing registry Secret in its own namespace; it must
    exist before the path is turned on, or the first Job fails on the image pull."""
    pem = value.get("brokerCaPem") if isinstance(value, dict) else None
    if (set(value) != {"enabled", "brokerCaPem"} or value["enabled"] is not True or not isinstance(pem, str)
            or not pem.startswith("-----BEGIN CERTIFICATE-----") or "PRIVATE KEY" in pem):
        raise ValueError("initial_proposal_public_ca_required")
    get("secret", INITIAL_PROPOSAL_REGISTRY, "bravo-initial")


WORKFLOW_KEYS = {"configuration", "reader", "stateClaim", "secrets", "interval", "applyCredential"}
APPLY_PLUGIN, APPLY_CONFIG = "/usr/local/bin/bravo-credential", "/t31/registration.json"


def verify_apply_credential(get, name, secrets):
    """T31 apply without a stored token: the ConfigMap holds only the registration and the kubeconfig
    that credentials.kubeconfig() makes for the in-Pod paths (exec plugin, 600 s Secret-bound tokens)."""
    sys.path.insert(0, str(ROOT / "infra/envs/rehearsal-access"))
    import credentials

    if secrets.get("applyKubeconfig"):
        raise ValueError("workflow_host_apply_credential_exclusive")
    data = get("configmap", name, "platform-state").get("data", {})
    if set(data) != {"kubeconfig", "registration.json"}:
        raise ValueError("workflow_host_apply_credential_fields")
    config = json.loads(data["registration.json"])
    if set(config) != {"registration", "anchor_uid"} or str(UUID(config["anchor_uid"])) != config["anchor_uid"]:
        raise ValueError("workflow_host_apply_credential_fields")
    registration = credentials.Registration.parse(config["registration"])
    if json.loads(data["kubeconfig"]) != credentials.kubeconfig(registration, APPLY_CONFIG, APPLY_PLUGIN):
        raise ValueError("workflow_host_apply_credential_exec_only")


def verify_workflow_host(get, host):
    """ADR-0031 host: every referenced input already exists; the ports, relay and Reader v2 configs
    are present, and the registration image (Reader packages + git) is published."""
    if set(host) != {"enabled", "runtime"} or host["enabled"] is not True:
        raise ValueError("protected_workflow_host_input_required")
    runtime = host["runtime"]
    if (not set(runtime) <= WORKFLOW_KEYS or not {"configuration", "reader", "stateClaim", "secrets"} <= set(runtime)
            or not runtime["secrets"].get("executor")):
        raise ValueError("workflow_host_runtime_fields_required")
    config = get("configmap", runtime["configuration"]["existingConfigMap"], "platform-state")
    if not {"ports.json", "relay.json"} <= set(config.get("data", {})):
        raise ValueError("workflow_host_ports_and_relay_required")
    # Same shape the relay enforces at run time (initial_proposal_relay.allowed), checked before turning
    # the host on (sangmu1126 #546): the platform Job principal and the named legacy fence principals.
    relay = json.loads(config["data"]["relay.json"])
    principals = relay.get("fence_principals") if isinstance(relay, dict) else None
    if (not isinstance(relay, dict) or set(relay) != {"job_principal", "fence_principals"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(relay["job_principal"]))
            or type(principals) is not list or not 1 <= len(principals) <= 4
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(p)) for p in principals)
            or relay["job_principal"] in principals):
        raise ValueError("workflow_host_relay_principals_invalid")
    reader = get("configmap", runtime["reader"]["existingConfigMap"], "platform-state")
    if json.loads(reader.get("data", {}).get("reader.json", "{}")).get("version") != 2:
        raise ValueError("workflow_host_reader_v2_required")
    for claim in (runtime["reader"]["catalogClaim"], runtime["stateClaim"]):
        get("pvc", claim, "platform-state")
    for name in {v for v in runtime["secrets"].values() if v}:
        get("secret", name, "platform-state")
    if runtime.get("applyCredential", {}).get("existingConfigMap"):
        verify_apply_credential(get, runtime["applyCredential"]["existingConfigMap"], runtime["secrets"])
    image = json.loads((ROOT / "infra/gitops/environments/development/registration-image.json").read_text())
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image["image"]["digest"]):
        raise ValueError("published_registration_image_required")


READER_COMMAND = ["python", "-m", "bravo_platform_events.product_read_server"]
READER_ARGS = ["--config", "/product-read/reader.json", "--tls-cert", "/tls/tls.crt", "--tls-key", "/tls/tls.key",
               "--initial-proposal"]


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
    adopted = tracking.startswith("platform-product-reader:")
    if (digest != runtime["configuration"]["sha256"]
            or deployment["spec"]["selector"]["matchLabels"] != labels
            or service["spec"]["selector"] != labels
            or service["spec"]["type"] != "ClusterIP"
            or pod["serviceAccountName"] != "platform-runtime"
            or pod.get("automountServiceAccountToken") is not False
            or (tracking and not adopted)):
        raise ValueError("reader_adoption_boundary_mismatch")
    volumes = {v["name"]: v for v in pod["volumes"]}
    if volumes["catalog"]["persistentVolumeClaim"].get("readOnly") is not True:
        raise ValueError("reader_adoption_boundary_mismatch")
    if adopted:
        # After adoption Argo keeps the Deployment on the previous runtime values, so a
        # reviewed new ConfigMap or TLS Secret cannot already be mounted. The identities,
        # ownership, boundary and the new references' UIDs and content hash above suffice.
        return
    # First adoption: the live workload must already be what the chart renders.
    if annotations.get("bravo.dev/config-sha256") != digest:
        raise ValueError("reader_adoption_boundary_mismatch")
    if (volumes["config"]["configMap"]["name"] != runtime["configuration"]["existingConfigMap"]
            or volumes["catalog"]["persistentVolumeClaim"] != {"claimName": runtime["catalogClaim"], "readOnly": True}
            or any(volumes[name]["secret"]["secretName"] != runtime["secrets"][key]
                   for name, key in (("credential", "database"), ("database-ca", "databaseCA"),
                                     ("catalog-key", "catalogKey"), ("tls", "tls")))):
        raise ValueError("reader_mount_change_requires_separate_review")
    containers = pod["containers"]
    if (len(containers) != 1 or containers[0].get("command") != READER_COMMAND
            or containers[0].get("args") != READER_ARGS
            or [(p.get("name"), p.get("containerPort")) for p in containers[0].get("ports", [])] != [("https", 8443)]):
        raise ValueError("reader_mode_mismatch")


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
    # A failed upgrade leaves the previous revision serving; upgrading again is Helm's
    # recovery. A pending-* release may still be running, so it still stops here.
    if releases and releases[0]["status"] not in ("deployed", "failed"):
        raise ValueError("existing_release_not_settled; inspect before recovery")
    values = []
    runtime_hash = None
    if a.runtime_values:
        if a.runtime_values.resolve().is_relative_to(ROOT):
            raise ValueError("runtime_input_must_remain_outside_checkout")
        value = json.loads(a.runtime_values.read_text())
        if (
            not {"registration"}.issubset(value)
            or not set(value).issubset({"registration", "productReader", "rehearsalHost", "initialProposal",
                                        "workflowHost"})
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
        config = get(
            "configmap", runtime["configuration"]["existingConfigMap"], "platform-state"
        )
        # The private catalog keeps its environment references; the profile allowlist is declared here.
        declared = json.loads(
            (ROOT / "infra/gitops/environments/development/registration-profiles.json").read_text()
        )["evaluation_profiles"]
        catalog = json.loads(config.get("data", {}).get("catalog.json", "{}"))
        key = lambda p: (p["tenant_id"], p["profile_id"], p["profile_version"])
        if sorted(map(key, catalog.get("evaluation_profiles", []))) != sorted(map(key, declared)):
            raise ValueError("registration_profiles_drift")
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
        if "rehearsalHost" in value:
            verify_rehearsal_host(get, value["rehearsalHost"])
        if "initialProposal" in value:
            verify_initial_proposal(get, value["initialProposal"])
        if "workflowHost" in value:
            verify_workflow_host(get, value["workflowHost"])
        values = ["--values", str(a.runtime_values)]
    # Reviewed, non-secret environment settings (public entry and its read-only UI) come from Git. The runtime
    # input is limited to the workload keys checked above, so it cannot change them.
    values = ["--values", str(ROOT / "infra/gitops/environments/development/public-entry.json"), *values]
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
        "registration_enabled": runtime_hash is not None,
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
                # Earlier releases left a client-side "helm" Update record next to Helm 4's
                # server-side Apply on the same fields. This release is their only writer;
                # Argo writes Application status only, so take the fields back.
                "--force-conflicts",
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
