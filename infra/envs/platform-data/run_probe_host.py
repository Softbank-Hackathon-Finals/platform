"""Operator entrypoint for fixed T7 verification jobs; no general runtime deploy permission."""
import hashlib
import json
import re
import subprocess
import sys
import time

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml"]


def kube(*args, data=None):
    p = subprocess.run(K + list(args), input=None if data is None else json.dumps(data),
                       text=True, capture_output=True, check=False, timeout=120)
    if p.returncode:
        raise RuntimeError("kubernetes_probe_operation_failed")
    return p.stdout


def main():
    settings = json.load(sys.stdin)
    action = settings["action"]
    if action == "restart_database":
        old = json.loads(kube("get", "pod", "platform-db-1", "-n", "platform-state", "-o", "json"))
        if old["metadata"]["labels"].get("cnpg.io/cluster") != "platform-db":
            raise RuntimeError("database_pod_ownership_mismatch")
        old_uid = old["metadata"]["uid"]
        kube("delete", "pod", "platform-db-1", "-n", "platform-state", "--wait=true", "--timeout=90s")
        for _ in range(90):
            text = kube("get", "pod", "platform-db-1", "-n", "platform-state", "--ignore-not-found", "-o", "json")
            if text:
                new = json.loads(text)
                if new["metadata"]["uid"] != old_uid and any(c["type"] == "Ready" and c["status"] == "True" for c in new["status"].get("conditions", [])):
                    print(json.dumps({"database_pod_recreated": True, "ready": True,
                        "old_uid_sha256": hashlib.sha256(old_uid.encode()).hexdigest(),
                        "new_uid_sha256": hashlib.sha256(new["metadata"]["uid"].encode()).hexdigest()}))
                    return
            time.sleep(2)
        raise RuntimeError("database_recovery_timeout")
    name = settings["name"]
    if not re.fullmatch(r"t7-[a-z0-9-]{1,48}", name):
        raise RuntimeError("invalid_probe_name")
    if action == "apply":
        manifest = settings["manifest"]
        for item in manifest["items"]:
            if item["metadata"].get("namespace") != "platform-state" or item["kind"] not in ("Job", "ConfigMap", "NetworkPolicy"):
                raise RuntimeError("probe_scope_mismatch")
            if not item["metadata"]["name"].startswith("t7-"):
                raise RuntimeError("probe_resource_name_mismatch")
        kube("wait", "--for=condition=Ready", "cluster/platform-db", "-n", "platform-state", "--timeout=90s")
        kube("apply", "--server-side", "--dry-run=server", "--field-manager=t7-platform-probe", "-f", "-", data=manifest)
        kube("apply", "--server-side", "--field-manager=t7-platform-probe", "-f", "-", data=manifest)
        print(json.dumps({"probe_job_applied": True, "name": name}))
    elif action == "read":
        job = json.loads(kube("get", "job", name, "-n", "platform-state", "-o", "json"))
        if not job["status"].get("succeeded") and not job["status"].get("failed"):
            print(json.dumps({"status": "RUNNING"}))
            return
        logs = kube("logs", "job/" + name, "-n", "platform-state")
        # Only the probe's JSON record is public. Dependency/driver text is not emitted.
        lines = [line for line in logs.splitlines() if line.startswith('{"status":')]
        if len(lines) != 1:
            raise RuntimeError("probe_receipt_missing_or_ambiguous")
        result = json.loads(lines[0])
        print(json.dumps({"job_succeeded": bool(job["status"].get("succeeded")), "probe": result}))
    else:
        raise RuntimeError("unknown_action")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__,
                          "error_code": str(error) if type(error) is RuntimeError else "invalid_input"}))
        raise SystemExit(1) from None
