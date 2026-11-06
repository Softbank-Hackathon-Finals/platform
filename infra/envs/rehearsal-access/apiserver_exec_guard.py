# /// script
# dependencies = []
# ///
"""The exec guard on a real API server: T3 templates + exec_guard.py in a throwaway k3s.

    uv run --script infra/envs/rehearsal-access/apiserver_exec_guard.py

Needs Docker. Every request is made as the namespace's `rehearsal-exec` (server dry-run for
creates/deletes, a real exec for CONNECT). The k3s container is removed afterwards.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
K3S = "rancher/k3s@sha256:5e0707cfd1239b358ef73f3254bc3eadc027dd30cd5ec6ca41e29e47652a1b8c"  # v1.34.1-k3s1
BUSYBOX = "busybox@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec"  # 1.37
NS = "rehearsal-a1"
NS2 = "rehearsal-a2"  # no tool Pod: a refusal there cannot be AlreadyExists
AS = f"--as=system:serviceaccount:{NS}:rehearsal-exec"


def sh(*args, check=True, stdin=None):
    result = subprocess.run(args, input=stdin, capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise SystemExit(f"setup failed: {' '.join(args[:4])}: {result.stderr.strip()[:400]}")
    return result


def pod(name="app", sa="default", automount=False, volumes=None, env=None, omit=()):
    # PSA restricted (templates/00-namespace.yaml) applies to everyone, so test Pods comply.
    restricted = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    spec = {"serviceAccountName": sa, "automountServiceAccountToken": automount,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "c", "image": BUSYBOX, "command": ["sleep", "600"],
                            "securityContext": restricted, **({"env": env} if env else {})}],
            **({"volumes": volumes} if volumes else {})}
    for key in omit:
        spec.pop(key)
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS}, "spec": spec}


CASES = [  # (description, manifest, expected admitted)
    ("plain pod, explicit default SA and no token", pod(), True),
    ("configMap volume", pod(volumes=[{"name": "c", "configMap": {"name": "cfg"}}]), True),
    ("secret volume", pod(volumes=[{"name": "s", "secret": {"secretName": "app"}}]), False),
    ("projected secret", pod(volumes=[{"name": "p", "projected": {"sources": [{"secret": {"name": "app"}}]}}]), False),
    ("secretKeyRef env", pod(env=[{"name": "P", "valueFrom": {"secretKeyRef": {"name": "app", "key": "p"}}}]), False),
    ("projected serviceAccountToken", pod(volumes=[{"name": "t", "projected": {"sources": [
        {"serviceAccountToken": {"path": "t"}}]}}]), False),
    ("other SA, automount omitted", pod(sa="other", omit=("automountServiceAccountToken",)), False),
    ("automount omitted on default", pod(omit=("automountServiceAccountToken",)), False),
    ("CSI inline volume", pod(volumes=[{"name": "c", "csi": {"driver": "secrets-store.csi.k8s.io"}}]), False),
]


def main():
    name = f"bravo-guard-k3s-{os.getpid()}"
    sh("docker", "run", "-d", "--privileged", "--name", name, "-p", "127.0.0.1::6443", K3S, "server",
       "--disable", "traefik", "--disable", "metrics-server", "--disable", "servicelb")
    work = pathlib.Path(tempfile.mkdtemp())
    kube = work / "kubeconfig"
    failures = []
    try:
        port = sh("docker", "port", name, "6443/tcp").stdout.strip().rsplit(":", 1)[1]
        for _ in range(90):
            got = sh("docker", "exec", name, "cat", "/etc/rancher/k3s/k3s.yaml", check=False)
            if got.returncode == 0:
                kube.write_text(got.stdout.replace("127.0.0.1:6443", f"127.0.0.1:{port}"))
                if sh("kubectl", f"--kubeconfig={kube}", "get", "--raw", "/readyz", check=False).returncode == 0:
                    break
            time.sleep(2)
        k = ["kubectl", f"--kubeconfig={kube}"]
        for namespace in (NS, NS2):
            for template in sorted((ROOT / "services/rehearsal-runner/templates").glob("[012]0-*.yaml")):
                sh(*k, "apply", "-f", "-", stdin=template.read_text().replace("${NAMESPACE}", namespace))
        guard = sh(sys.executable, str(ROOT / "infra/envs/rehearsal-access/exec_guard.py")).stdout
        sh(*k, "apply", "-f", "-", stdin=guard)
        sh(*k, "-n", NS, "create", "secret", "generic", "app", "--from-literal=p=private")
        sh(*k, "-n", NS, "create", "serviceaccount", "other")  # automount defaults to true
        sh(*k, "-n", NS, "create", "configmap", "cfg", "--from-literal=a=b")
        tool = pod(name="rehearsal-exec-tool")  # created by the runner (admin), as in production
        sh(*k, "apply", "-f", "-", stdin=json.dumps(tool))
        app = pod(name="demo-chat", volumes=[{"name": "s", "secret": {"secretName": "app"}}])
        sh(*k, "apply", "-f", "-", stdin=json.dumps(app))
        sh(*k, "-n", NS, "wait", "--for=condition=Ready", "pod/rehearsal-exec-tool", "pod/demo-chat", "--timeout=180s")
        time.sleep(3)  # admission policies are watched asynchronously

        def admitted(*args, stdin=None, as_user=AS):
            result = sh(*k, as_user, *args, stdin=stdin, check=False)
            # A refusal counts only when our admission policy made it (not PSA, RBAC or a conflict).
            if result.returncode and "ValidatingAdmissionPolicy" not in result.stderr:
                print("     (not the guard:", result.stderr.strip().splitlines()[-1][:160], ")")
                return None
            return result.returncode == 0

        results = []
        for description, manifest, expected in CASES:
            got = admitted("create", "--dry-run=server", "-f", "-", stdin=json.dumps(manifest))
            results.append((description, expected, got))
        other = pod(name="rehearsal-exec-tool") | {"metadata": {"name": "rehearsal-exec-tool", "namespace": NS2}}
        results.append(("create the tool Pod name where none exists", False, admitted(
            "create", "--dry-run=server", "-f", "-", stdin=json.dumps(other),
            as_user=f"--as=system:serviceaccount:{NS2}:rehearsal-exec")))
        results.append(("delete the tool Pod", False, admitted("-n", NS, "delete", "pod", "rehearsal-exec-tool",
                                                             "--dry-run=server")))
        results.append(("exec into the tool Pod", True, admitted("-n", NS, "exec", "rehearsal-exec-tool", "--",
                                                               "true")))
        results.append(("exec into an app Pod with a Secret", False, admitted("-n", NS, "exec", "demo-chat", "--",
                                                                            "cat", "/var/run/secrets/x")))
        for description, expected, got in results:
            mark = "ok  " if got is expected else "FAIL"
            print(f"{mark} {'admitted' if got else 'refused '}  {description}")
            if got is not expected:
                failures.append(description)
        version = sh(*k, "version", "-o", "json").stdout
        print("server", json.loads(version)["serverVersion"]["gitVersion"])
    finally:
        sh("docker", "rm", "-f", name, check=False)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
