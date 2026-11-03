"""Read-only host probe. Never print configuration, certificates, tokens or logs."""

import json
import os
import re
import subprocess
from pathlib import Path


def command(args, timeout=20):
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "KUBECONFIG": "/etc/rancher/rke2/rke2.yaml"})
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def json_command(args):
    code, body = command(args)
    if code:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def collect():
    kube = "/var/lib/rancher/rke2/bin/kubectl"
    nodes = json_command([kube, "get", "nodes", "-o", "json", "--request-timeout=10s"])
    pods = json_command([kube, "get", "pods", "-n", "kube-system", "-o", "json", "--request-timeout=10s"])
    version = command(["/usr/local/bin/rke2", "--version"])[1]
    match = re.search(r"\bv\d+\.\d+\.\d+\+rke2r\d+\b", version)
    config = Path("/etc/rancher/rke2/config.yaml")
    kubeconfig = Path("/etc/rancher/rke2/rke2.yaml")
    subject = "system:serviceaccount:platform-smoke:deployer"

    def can_i(verb, resource, namespace):
        code, value = command([kube, "auth", "can-i", verb, resource, "-n", namespace,
                               "--as=" + subject, "--request-timeout=10s"])
        value = value.strip()
        return value if code in (0, 1) and value in ("yes", "no") else "UNKNOWN"

    cloud = json_command(["cloud-init", "status", "--format", "json"])
    cloud_status = (cloud or {}).get("status")
    if cloud_status not in ("done", "running", "error", "disabled", "not run"):
        cloud_status = "UNKNOWN"
    node_items = (nodes or {}).get("items", [])
    pod_items = (pods or {}).get("items", [])
    curl_code, curl_output = command(["curl", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
                                      "--connect-timeout", "5", "--max-time", "10", "https://ghcr.io/v2/"])
    return {
        "schema_version": 1,
        "cloud_init_status": cloud_status,
        "bootstrap_marker_exists": Path("/var/lib/bravo-runtime-ready").is_file(),
        "rke2_service_active": command(["systemctl", "is-active", "--quiet", "rke2-server"])[0] == 0,
        "rke2_version": match[0] if match else None,
        "cis_profile_configured": bool(config.is_file() and re.search(r"(?m)^profile:\s*cis\s*$", config.read_text())),
        "kubeconfig_owner_only": kubeconfig.is_file() and kubeconfig.stat().st_mode & 0o777 == 0o600,
        "api_ready": command([kube, "get", "--raw=/readyz", "--request-timeout=10s"])[0] == 0,
        "nodes_query_succeeded": nodes is not None,
        "node_count": len(node_items),
        "ready_node_count": sum(any(c.get("type") == "Ready" and c.get("status") == "True" for c in x.get("status", {}).get("conditions", [])) for x in node_items),
        "system_pods_query_succeeded": pods is not None,
        "system_pod_count": len(pod_items),
        "system_pods_pending_or_failed": sum(x.get("status", {}).get("phase") in ("Pending", "Failed", "Unknown") for x in pod_items),
        "smoke_can_get_secret": can_i("get", "secrets", "platform-smoke"),
        "smoke_can_create_configmap": can_i("create", "configmaps", "platform-smoke"),
        "smoke_can_write_other_namespace": can_i("create", "configmaps", "default"),
        "ghcr_http_status": curl_output if curl_code == 0 and re.fullmatch(r"\d{3}", curl_output) else None,
        "human_kubernetes_login_verified": False,
        "ghcr_image_pull_verified": False,
        "cis_benchmark_verified": False,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(collect(), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error_code": "host_probe_failed"}))
        raise SystemExit(1) from None
