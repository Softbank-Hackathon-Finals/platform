"""Check platform identities against the real API, plus DB isolation without credentials."""
import copy
import json
import subprocess

PYTHON = "docker.io/library/python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
NETWORK_PROBE = """import json,os,socket
try:
 s=socket.create_connection((os.environ['DB_IP'],5432),timeout=5);s.close()
 print(json.dumps({'database_network_denied':False}));raise SystemExit(1)
except TimeoutError:
 print(json.dumps({'database_network_denied':True}))
"""

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml"]


def admin(*args):
    return subprocess.check_output(K + list(args), text=True)


def main():
    config = json.loads(admin("config", "view", "--raw", "-o", "json"))
    checks = {}
    for namespace, account in [("platform-state", "platform-runtime"),
                                ("platform-collector", "platform-collector"),
                                ("platform-executor", "platform-executor")]:
        token = admin("create", "token", account, "-n", namespace, "--duration=10m").strip()
        child = copy.deepcopy(config)
        child["users"] = [{"name": "probe", "user": {"token": token}}]
        child["contexts"] = [{"name": "probe", "context": {"cluster": config["contexts"][0]["context"]["cluster"], "user": "probe"}}]
        child["current-context"] = "probe"
        for target, resource in [("platform-state", "secrets"), ("platform-state", "pods"), ("kube-system", "secrets")]:
            p = subprocess.run([K[0], "--kubeconfig=/dev/stdin", "get", resource, "-n", target],
                               input=json.dumps(child), text=True, capture_output=True, check=False, timeout=20)
            checks[f"{account}_denied_{target}_{resource}"] = p.returncode != 0 and "forbidden" in p.stderr.lower()
    address = json.loads(admin("get", "service", "platform-db-rw", "-n", "platform-state", "-o", "json"))["spec"]["clusterIP"]
    for namespace in ("platform-collector", "platform-executor"):
        name = "t7-db-network-denial"
        job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": namespace},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 90, "template": {"spec": {
                "serviceAccountName": namespace, "automountServiceAccountToken": False, "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "probe", "image": PYTHON, "command": ["python", "-c", NETWORK_PROBE],
                    "env": [{"name": "DB_IP", "value": address}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}}}]}}}}
        subprocess.run(K + ["create", "-f", "-"], input=json.dumps(job), capture_output=True, text=True, check=True)
        try:
            admin("wait", "--for=condition=Complete", "job/" + name, "-n", namespace, "--timeout=90s")
            result = json.loads(admin("logs", "job/" + name, "-n", namespace))
            checks[namespace + "_database_network_denied"] = result == {"database_network_denied": True}
        finally:
            # This fixed name is created by this invocation; never delete foreign workloads.
            admin("delete", "job", name, "-n", namespace, "--wait=true", "--timeout=60s")
    print(json.dumps({"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
                      "auth": "actual_TokenRequest_10m", "check_count": len(checks)}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
