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
            set(value) != {"registration"}
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
