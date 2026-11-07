"""kubectl with a platform-held kubeconfig and an identity check.

Secret values and SQL travel on stdin only, never in argv (argv shows up in process listings and
audit logs). Errors keep the first stderr line without echoing stdin.
"""

import json
import subprocess
import time
from collections.abc import Sequence

FIELD_MANAGER = "bravo-migration"


class KubeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class IdentityMismatch(KubeError):
    pass


class Kube:
    def __init__(self, kubeconfig: str, cluster_uid: str, *, kubectl: str = "kubectl", timeout_s: float = 60):
        self._base = [kubectl, "--kubeconfig", kubeconfig]
        self.cluster_uid = cluster_uid
        self._timeout = timeout_s

    def run(self, *args: str, input: str | None = None, timeout_s: float | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run([*self._base, *args], input=input, capture_output=True, text=True,
                                    timeout=timeout_s or self._timeout)
        except subprocess.TimeoutExpired as e:
            raise KubeError("kube_timeout", f"kubectl {args[0]} timed out") from e
        if check and result.returncode != 0:
            first = (result.stderr or result.stdout).strip().splitlines()
            raise KubeError("kube_failed", f"kubectl {args[0]} failed: {first[0][:300] if first else result.returncode}")
        return result

    def verify_identity(self) -> None:
        uid = self.run("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}", timeout_s=15).stdout
        if uid.strip() != self.cluster_uid:
            raise IdentityMismatch("cluster_identity_mismatch",
                                   "the cluster behind this kubeconfig is not the registered one")

    def get(self, kind: str, name: str | None = None, namespace: str | None = None,
            selector: str | None = None) -> dict | None:
        args = ["get", kind]
        if name:
            args.append(name)
        if namespace:
            args += ["-n", namespace]
        if selector:
            args += ["-l", selector]
        result = self.run(*args, "-o", "json", check=False)
        if result.returncode != 0:
            if "NotFound" in result.stderr or "not found" in result.stderr:
                return None
            raise KubeError("kube_failed", f"kubectl get {kind} failed: {result.stderr.strip()[:300]}")
        return json.loads(result.stdout)

    def apply(self, objects: Sequence[dict]) -> None:
        body = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)})
        self.run("apply", "--server-side", "--force-conflicts", f"--field-manager={FIELD_MANAGER}", "-f", "-",
                 input=body, timeout_s=120)

    def exec(self, namespace: str, target: str, argv: Sequence[str], *, container: str | None = None,
             input: str | None = None, timeout_s: float = 60, check: bool = True) -> subprocess.CompletedProcess:
        args = ["exec", "-n", namespace, "-i" if input is not None else "", target]
        if container:
            args += ["-c", container]
        args = [a for a in args if a]
        return self.run(*args, "--", *argv, input=input, timeout_s=timeout_s, check=check)

    def running_pods(self, namespace: str, selector: str) -> list[dict]:
        pods = self.get("pods", namespace=namespace, selector=selector) or {"items": []}
        return [p for p in pods["items"]
                if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]

    def ready_pods(self, namespace: str, selector: str) -> list[dict]:
        ready = []
        for p in self.running_pods(namespace, selector):
            conditions = {c["type"]: c["status"] for c in p["status"].get("conditions", [])}
            if conditions.get("Ready") == "True":
                ready.append(p)
        return ready

    def rollout(self, namespace: str, deployment: str, timeout_s: int = 300) -> None:
        self.run("rollout", "status", "-n", namespace, f"deployment/{deployment}", f"--timeout={timeout_s}s",
                 timeout_s=timeout_s + 15)

    def wait_job(self, namespace: str, name: str, timeout_s: int = 300) -> str:
        """'Complete' or 'Failed'; raises KubeError on timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            job = self.get("job", name, namespace)
            for c in (job or {}).get("status", {}).get("conditions", []):
                if c["status"] == "True" and c["type"] in ("Complete", "Failed"):
                    return c["type"]
            time.sleep(2)
        raise KubeError("job_timeout", f"job {namespace}/{name} did not finish in {timeout_s}s")

    def node_image_digests(self) -> set[str]:
        nodes = self.get("nodes") or {"items": []}
        digests = set()
        for n in nodes["items"]:
            for image in n["status"].get("images", []):
                for name in image.get("names", []):
                    if "@sha256:" in name:
                        digests.add(name.split("@", 1)[1])
        return digests

    def secret_value(self, namespace: str, name: str, key: str) -> str | None:
        import base64

        secret = self.get("secret", name, namespace)
        if secret is None or key not in secret.get("data", {}):
            return None
        return base64.b64decode(secret["data"][key]).decode()
