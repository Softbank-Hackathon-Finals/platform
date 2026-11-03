"""Verify actual short-lived collector/Executor tokens; never persist or print tokens."""
import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path


def call(config, *args):
    return subprocess.run(["kubectl", "--kubeconfig=/dev/stdin", "--request-timeout=15s", *args],
                          input=json.dumps(config), capture_output=True, text=True, check=False, timeout=30)


def success(config, *args):
    p = call(config, *args)
    if p.returncode:
        raise RuntimeError("kubernetes_request_failed")
    return p.stdout


def exec_denial(config, namespace, pods):
    """Query both exec transports on each actual Pod, without opening a session."""
    observations = []
    for pod in sorted({item['metadata']['name'] for item in pods}):
        for verb in ('get', 'create'):
            decision = 'UNKNOWN'
            try:
                result = call(config, 'auth', 'can-i', verb, 'pods/' + pod,
                              '--subresource=exec', '-n', namespace)
                if result.returncode == 0 and result.stdout.strip() == 'yes':
                    decision = 'ALLOW'
                elif result.returncode == 1 and result.stdout.strip() == 'no':
                    decision = 'DENY'
            except (OSError, subprocess.SubprocessError):
                pass
            observations.append({'pod_sha256': hashlib.sha256(pod.encode()).hexdigest(),
                                 'verb': verb, 'decision': decision})
    denied = None
    if any(row['decision'] == 'ALLOW' for row in observations):
        denied = False
    elif observations and all(row['decision'] == 'DENY' for row in observations):
        denied = True
    return {'denied': denied, 'observations': observations}


def verify(admin, namespace, configmap):
    original = json.loads(success(admin, "get", "configmap", configmap, "-n", namespace, "-o", "json"))
    key = "bravo.dev/t7-access-probe"
    if key in original["metadata"].get("annotations", {}):
        raise RuntimeError("probe_annotation_already_owned")
    checks, exec_authorization = {}, {}
    for identity in ("collector", "executor"):
        token = success(admin, "create", "token", "bravo-" + identity, "-n", namespace, "--duration=10m").strip()
        config = copy.deepcopy(admin)
        config["users"] = [{"name": "probe", "user": {"token": token}}]
        config["contexts"] = [{"name": "probe", "context": {"cluster": admin["contexts"][0]["context"]["cluster"], "user": "probe"}}]
        config["current-context"] = "probe"
        result = json.loads(success(config, "get", "pods", "-n", namespace, "-o", "json"))
        checks[identity + "_can_read_workload"] = bool(result["items"])
        denied = {
            "secret_read": ["get", "secrets", "-n", namespace],
            "other_namespace": ["get", "pods", "-n", "kube-system"],
            "token_escalation": ["create", "token", "bravo-executor", "-n", namespace, "--duration=10m"],
            "pod_create": ["run", "t7-forbidden", "--image=busybox:1.37", "--dry-run=server", "-n", namespace],
            "other_config_write": ["patch", "configmap", "not-authorized", "-n", namespace, "--dry-run=server", "--type=merge", "-p", '{"metadata":{"annotations":{"probe":"1"}}}'],
        }
        if identity == "collector":
            denied["pooler_write"] = ["patch", "configmap", configmap, "-n", namespace, "--dry-run=server", "--type=merge", "-p", '{"metadata":{"annotations":{"probe":"1"}}}']
        for name, args in denied.items():
            p = call(config, *args)
            checks[identity + "_denied_" + name] = p.returncode != 0 and "forbidden" in p.stderr.lower()
        exec_authorization[identity] = exec_denial(config, namespace, result["items"])
        checks[identity + "_denied_exec"] = exec_authorization[identity]["denied"]
        if identity == "executor":
            patch = json.dumps({"metadata": {"annotations": {key: "verified"}}})
            try:
                success(config, "patch", "configmap", configmap, "-n", namespace, "--type=merge", "-p", patch)
                checks["executor_exact_config_patch"] = True
            finally:
                # Remove only our annotation; never restore/overwrite the whole object.
                success(admin, "patch", "configmap", configmap, "-n", namespace, "--type=merge", "-p",
                        json.dumps({"metadata": {"annotations": {key: None}}}))
    after = json.loads(success(admin, "get", "configmap", configmap, "-n", namespace, "-o", "json"))
    checks["pooler_data_unchanged"] = original.get("data") == after.get("data") and original.get("binaryData") == after.get("binaryData")
    checks["other_annotations_unchanged"] = original["metadata"].get("annotations", {}) == after["metadata"].get("annotations", {})
    status = "PASS"
    if any(value is False for value in checks.values()):
        status = "FAIL"
    elif any(value is None for value in checks.values()):
        status = "UNKNOWN"
    return {"status": status, "checks": checks, "exec_authorization": exec_authorization, "check_count": len(checks),
            "auth": "actual_TokenRequest_10m", "pooler_data_sha256": hashlib.sha256(json.dumps(after.get("data"), sort_keys=True).encode()).hexdigest(),
            "cutover_executed": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", type=Path, required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--pooler-config", required=True)
    args = p.parse_args()
    try:
        result = verify(json.loads(args.kubeconfig.read_text()), args.namespace, args.pooler_config)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "PASS" else 1)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
