"""Host-local, bounded kubelet pull test; private image config arrives on stdin."""

import json
import math
import pathlib
import re
import subprocess
import sys
import time

KUBE = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig", "/etc/rancher/rke2/rke2.yaml"]
NAMESPACE = "t7-registry-check"
LABEL = {"bravo.platform/purpose": "private-image-verification"}


class Failure(RuntimeError):
    pass


def call(args, *, obj=None):
    result = subprocess.run(KUBE + args, check=False, input=json.dumps(obj) if obj is not None else None,
                            text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise Failure("kubernetes_operation_failed")
    return result.stdout


def load(args):
    return json.loads(call(args))


def metric_calls(node):
    raw = call(["get", "--raw=/api/v1/nodes/" + node + "/proxy/metrics"])
    values = re.findall(
        r'^kubelet_credential_provider_plugin_duration_count\{plugin_name="ecr-credential-provider"\}'
        r'[ \t]+([^\s]+)(?:[ \t]+[0-9]+)?[ \t]*$', raw, re.MULTILINE)
    if len(values) != 1:
        return None
    try:
        count = float(values[0])
    except ValueError:
        return None
    return int(count) if math.isfinite(count) and count >= 0 and count.is_integer() else None


def main():
    config = json.load(sys.stdin)
    repository, digest = config["repository_url"], config["digest"]
    if not re.fullmatch(r"[0-9]{12}\.dkr\.ecr\.ap-northeast-2\.amazonaws\.com/bravo/demo-chat", repository):
        raise Failure("repository_boundary_invalid")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) or not re.fullmatch(r"sha256:[a-f0-9]{64}", config["platform_digest"]):
        raise Failure("digest_invalid")
    if pathlib.Path("/etc/rancher/rke2/registries.yaml").exists():
        raise Failure("existing_registry_configuration_needs_owner_review")
    nodes = load(["get", "nodes", "-o", "json"])["items"]
    if len(nodes) != 1:
        raise Failure("unexpected_node_inventory")
    node = nodes[0]["metadata"]["name"]
    namespaces = load(["get", "namespaces", "-o", "json"])["items"]
    existing = next((n for n in namespaces if n["metadata"]["name"] == NAMESPACE), None)
    if existing and any(existing["metadata"].get("labels", {}).get(k) != v for k, v in LABEL.items()):
        raise Failure("namespace_owner_mismatch")
    if not existing:
        call(["create", "-f", "-"], obj={"apiVersion": "v1", "kind": "Namespace", "metadata": {
            "name": NAMESPACE, "labels": {**LABEL, "pod-security.kubernetes.io/enforce": "restricted",
                                          "pod-security.kubernetes.io/enforce-version": "v1.35"}}})
    if load(["get", "pods", "-n", NAMESPACE, "-o", "json"])["items"]:
        raise Failure("existing_probe_pod_requires_review")
    call(["apply", "-f", "-"], obj={"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "deny-all", "namespace": NAMESPACE, "labels": LABEL},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}})
    for _ in range(20):
        accounts = load(["get", "serviceaccounts", "-n", NAMESPACE, "-o", "json"])["items"]
        if accounts:
            break
        time.sleep(1)
    if any(a.get("imagePullSecrets") for a in accounts):
        raise Failure("unexpected_namespace_pull_secret")
    before = metric_calls(node)
    probe = ("import json,os,app.main,psycopg.pq; assert os.getuid()==10001; "
             "assert psycopg.pq.version()>0; "
             "print(json.dumps({'uid':10001,'app_import':True,'postgres_driver':True,'external_service_calls':False}))")
    observations = []
    for suffix in ("a", "b"):
        name = "private-pull-" + suffix
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NAMESPACE, "labels": LABEL},
               "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{"name": "probe", "image": repository + "@" + digest, "imagePullPolicy": "Always",
                            "command": ["/app/.venv/bin/python", "-c", probe],
                            "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                            "resources": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}}]}}
        call(["create", "-f", "-"], obj=pod)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            actual = load(["get", "pod", name, "-n", NAMESPACE, "-o", "json"])
            if actual["status"]["phase"] in ("Succeeded", "Failed"):
                break
            time.sleep(3)
        if actual["status"]["phase"] != "Succeeded":
            raise Failure("private_pull_probe_not_successful")
        status = actual["status"]["containerStatuses"][0]
        actual_digest = re.search(r"sha256:[a-f0-9]{64}$", status["imageID"])
        if not actual_digest or actual_digest.group() not in (digest, config["platform_digest"]):
            raise Failure("runtime_digest_mismatch")
        if json.loads(call(["logs", name, "-n", NAMESPACE])) != {
            "uid": 10001, "app_import": True, "postgres_driver": True, "external_service_calls": False
        }:
            raise Failure("runtime_probe_output_invalid")
        events = load(["get", "events", "-n", NAMESPACE, "--field-selector", "involvedObject.uid=" + actual["metadata"]["uid"], "-o", "json"])["items"]
        pulled = any(e.get("reason") == "Pulled" and "Successfully pulled image" in e.get("message", "") for e in events)
        if not pulled:
            raise Failure("registry_pull_event_missing")
        observations.append({"phase": "Succeeded", "runtime_digest": actual_digest.group(), "registry_pull_event": pulled,
                             "image_pull_policy": "Always", "pull_secret_count": len(actual["spec"].get("imagePullSecrets", [])),
                             "service_account_token_mounted": actual["spec"].get("automountServiceAccountToken", True)})
        call(["delete", "pod", name, "-n", NAMESPACE, "--wait=true", "--timeout=30s"])
    after = metric_calls(node)
    result = {"private_ecr_kubelet_pull_verified": True, "source_digest": digest,
              "probe_pods": observations, "provider_metric_calls_before": before, "provider_metric_calls_after": after,
              "probe_pods_removed": True, "dedicated_namespace_and_deny_all_policy_retained": True,
              "registry_credentials_persisted": False, "app_database_access_tested": False,
              "credential_renewal_after_expiry_observed": False}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Failure as error:
        print(json.dumps({"pull": "NOT_VERIFIED", "error_code": str(error)}))
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        print(json.dumps({"pull": "NOT_VERIFIED", "error_code": "invalid_probe_state"}))
        raise SystemExit(1) from None
