"""Pin the target workload to the approved image digest and confirm the rollout.

No build happens here. The image T15 verified in the rehearsal is promoted as-is: building
again would produce a different digest and break the approval binding (ADR-0021). The
Kubernetes access is an adapter so tests use ``FakeKube`` and the real path is one small
``kubectl`` wrapper bound to a namespace-scoped kubeconfig.

Every cluster call is wrapped: a failure before the patch is reported as "nothing changed"
and a failure from the patch onward as "the cluster may have changed" (ADR-0021 §7). No
adapter error escapes as an unrecorded exception.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Protocol

from .binding import DIGEST


class DeployMismatch(RuntimeError):
    """The cluster did not end up running the approved digest.

    ``mutation_attempted`` is False only when the failure happened before the patch was sent,
    so the caller can tell "nothing changed" (REFUSED) from "may have changed" (SAFE_HOLD)."""

    def __init__(self, code: str, *, mutation_attempted: bool, previous_image: str | None = None,
                 observed: list[str] | None = None, detail: str = "", policy: dict | None = None):
        super().__init__(code)
        self.code = code
        self.mutation_attempted = mutation_attempted
        self.previous_image = previous_image
        self.observed = list(observed or [])
        self.detail = detail
        self.policy = policy  # bucket-policy branch: what was read, written and observed (no secret values)


class Kube(Protocol):
    cluster: str  # "kubectl" for a real cluster, "fake" for the in-memory one
    def current_image(self, namespace: str, deployment: str, container: str) -> str: ...
    def patch_image(self, namespace: str, deployment: str, patch: dict) -> None: ...
    def rollout_status(self, namespace: str, deployment: str, timeout_seconds: int) -> bool: ...
    def running_digests(self, namespace: str, deployment: str, container: str) -> list[str]: ...
    # bucket-policy branch (policy.py): one Secret replaced, one workload rolled
    def get_secret(self, namespace: str, name: str) -> dict: ...
    def replace_secret(self, namespace: str, secret: dict) -> None: ...
    def restart_workload(self, namespace: str, deployment: str, annotations: dict[str, str]) -> None: ...


def image_patch(repository: str, digest: str, container: str) -> dict:
    """Strategic-merge patch that pins exactly ``repository@sha256:…``. Tags are never used."""
    if not DIGEST.fullmatch(digest):
        raise ValueError("image digest format")
    if "@" in repository or ":" in repository.rsplit("/", 1)[-1]:
        raise ValueError("repository must not carry a tag or digest")
    return {"spec": {"template": {"spec": {"containers": [{"name": container, "image": f"{repository}@{digest}"}]}}}}


def brief(exc: BaseException) -> str:
    """One short, code-free line for the step log. kubectl stderr carries no secret values."""
    if isinstance(exc, subprocess.CalledProcessError):
        first = ((exc.stderr or "").strip().splitlines() or [""])[0]
        return f"kubectl exit {exc.returncode}: {first}"[:240]
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"kubectl timed out after {exc.timeout}s"
    return f"{type(exc).__name__}: {exc}"[:240]


def apply_pinned_image(kube: Kube, *, namespace: str, deployment: str, container: str, repository: str,
                       digest: str, timeout_seconds: int = 300) -> dict:
    """Read, patch, wait, then verify every running pod reports the approved digest.

    Raises ``DeployMismatch`` for every way this can go wrong, including adapter errors."""
    patch = image_patch(repository, digest, container)
    try:
        before = kube.current_image(namespace, deployment, container)
    except Exception as exc:  # noqa: BLE001 - any read failure means "nothing was changed"
        raise DeployMismatch("CLUSTER_READ_FAILED", mutation_attempted=False, detail=brief(exc)) from exc
    try:
        kube.patch_image(namespace, deployment, patch)
    except Exception as exc:  # noqa: BLE001 - the API server may have accepted it anyway
        raise DeployMismatch("PATCH_FAILED", mutation_attempted=True, previous_image=before, detail=brief(exc)) from exc
    try:
        complete = kube.rollout_status(namespace, deployment, timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        raise DeployMismatch("ROLLOUT_STATUS_FAILED", mutation_attempted=True, previous_image=before,
                             observed=_observe(kube, namespace, deployment, container), detail=brief(exc)) from exc
    if not complete:
        raise DeployMismatch("ROLLOUT_NOT_COMPLETE", mutation_attempted=True, previous_image=before,
                             observed=_observe(kube, namespace, deployment, container))
    try:
        running = kube.running_digests(namespace, deployment, container)
    except Exception as exc:  # noqa: BLE001
        raise DeployMismatch("RUNNING_DIGEST_UNREADABLE", mutation_attempted=True, previous_image=before,
                             detail=brief(exc)) from exc
    if not running or any(d != digest for d in running):
        raise DeployMismatch("RUNNING_DIGEST_MISMATCH", mutation_attempted=True, previous_image=before, observed=running)
    return {"previous_image": before, "deployed_digest": digest, "pods": len(running), "observed": running}


def _observe(kube: Kube, namespace: str, deployment: str, container: str) -> list[str]:
    """Best-effort read of what is running, for the failure report. Never raises."""
    try:
        return list(kube.running_digests(namespace, deployment, container))
    except Exception:  # noqa: BLE001
        return []


class FakeKube:
    """In-memory cluster for unit tests only. Results it produces are marked ``cluster: fake``.

    ``rollout_result`` and ``drift`` model rollout failures; ``fail`` maps a method name to the
    exception that method raises, to model adapter errors."""

    cluster = "fake"

    def __init__(self, image: str, *, rollout_result: bool = True, drift: str | None = None,
                 fail: dict[str, BaseException] | None = None, secrets: dict[str, dict] | None = None):
        self.image, self.rollout_result, self.drift = image, rollout_result, drift
        self.fail = dict(fail or {})
        self.patches: list[dict] = []
        self.secrets = {name: json.loads(json.dumps(obj)) for name, obj in (secrets or {}).items()}
        self.secret_writes: list[dict] = []
        self.restarts: list[dict] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def current_image(self, namespace, deployment, container):
        self._maybe_fail("current_image")
        return self.image

    def patch_image(self, namespace, deployment, patch):
        self.patches.append(patch)
        self._maybe_fail("patch_image")
        self.image = patch["spec"]["template"]["spec"]["containers"][0]["image"]

    def rollout_status(self, namespace, deployment, timeout_seconds):
        self._maybe_fail("rollout_status")
        return self.rollout_result

    def running_digests(self, namespace, deployment, container):
        self._maybe_fail("running_digests")
        digest = self.drift or self.image.split("@", 1)[-1]
        return [digest, digest]

    def get_secret(self, namespace, name):
        self._maybe_fail("get_secret")
        if name not in self.secrets:
            raise LookupError(f"secret {name!r} not found")
        return json.loads(json.dumps(self.secrets[name]))

    def replace_secret(self, namespace, secret):
        self.secret_writes.append(secret)
        self._maybe_fail("replace_secret")
        self.secrets[secret["metadata"]["name"]] = json.loads(json.dumps(secret))

    def restart_workload(self, namespace, deployment, annotations):
        self.restarts.append(dict(annotations))
        self._maybe_fail("restart_workload")


class KubectlKube:
    """Real adapter. ``kubeconfig`` must be the namespace-scoped apply identity (no cluster admin).

    App image change: ``deployments`` get · watch · patch on the one app Deployment and ``pods``
    get · list. Bucket-policy change (a separate identity): ``secrets`` get · update on the one
    SeaweedFS auth Secret and ``deployments`` get · watch · patch on the one SeaweedFS workload."""

    cluster = "kubectl"
    CALL_SECONDS = 120

    def __init__(self, kubeconfig: str):
        self.kubeconfig = kubeconfig

    # client-go falls back to the Pod's in-cluster identity when the loaded kubeconfig is empty.
    # Without these variables that fallback cannot happen, whatever file is mounted.
    IN_CLUSTER_ENV = ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBECONFIG")

    def _run(self, *args: str, timeout: int | None = None, stdin: str | None = None) -> str:
        env = {k: v for k, v in os.environ.items() if k not in self.IN_CLUSTER_ENV}
        return subprocess.run(["kubectl", "--kubeconfig", self.kubeconfig, *args], check=True, input=stdin,
                              capture_output=True, text=True, timeout=timeout or self.CALL_SECONDS,
                              env=env).stdout

    def _deployment(self, namespace: str, deployment: str) -> dict[str, Any]:
        return json.loads(self._run("-n", namespace, "get", "deployment", deployment, "-o", "json"))

    def current_image(self, namespace, deployment, container):
        spec = self._deployment(namespace, deployment)["spec"]["template"]["spec"]
        image = next((c["image"] for c in spec["containers"] if c["name"] == container), None)
        if image is None:
            raise LookupError(f"container {container!r} not in deployment {deployment!r}")
        return image

    def patch_image(self, namespace, deployment, patch):
        self._run("-n", namespace, "patch", "deployment", deployment, "--type", "strategic", "-p", json.dumps(patch))

    def rollout_status(self, namespace, deployment, timeout_seconds):
        # The subprocess limit must outlast kubectl's own --timeout, or a slow rollout would
        # surface as TimeoutExpired instead of "not complete".
        try:
            self._run("-n", namespace, "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout_seconds}s",
                      timeout=timeout_seconds + 30)
            return True
        except subprocess.CalledProcessError:
            return False

    def running_digests(self, namespace, deployment, container):
        selector = ",".join(f"{k}={v}" for k, v in self._deployment(namespace, deployment)["spec"]["selector"]["matchLabels"].items())
        pods = json.loads(self._run("-n", namespace, "get", "pods", "-l", selector, "-o", "json"))["items"]
        out = []
        for pod in pods:
            # Pods of the old ReplicaSet can still report phase Running while terminating.
            if pod.get("metadata", {}).get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running":
                continue
            for status in pod.get("status", {}).get("containerStatuses", []):
                if status.get("name") == container:
                    image_id = status.get("imageID", "")
                    out.append(image_id.split("@", 1)[-1] if "@" in image_id else image_id)
        return out

    def get_secret(self, namespace, name):
        return json.loads(self._run("-n", namespace, "get", "secret", name, "-o", "json"))

    def replace_secret(self, namespace, secret):
        # The object goes through stdin, never argv or a file. It keeps the resourceVersion that was
        # read, so a concurrent change makes the server refuse instead of being overwritten.
        self._run("-n", namespace, "replace", "-f", "-", "-o", "name", stdin=json.dumps(secret))

    def restart_workload(self, namespace, deployment, annotations):
        patch = {"spec": {"template": {"metadata": {"annotations": annotations}}}}
        self._run("-n", namespace, "patch", "deployment", deployment, "--type", "strategic", "-p", json.dumps(patch))
