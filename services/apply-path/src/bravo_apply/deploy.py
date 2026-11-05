"""Pin the target workload to the approved image digest and confirm the rollout.

No build happens here. The image T15 verified in the rehearsal is promoted as-is: building
again would produce a different digest and break the approval binding (ADR-0021). The
Kubernetes access is an adapter so tests use ``FakeKube`` and the real path is one small
``kubectl`` wrapper bound to a namespace-scoped kubeconfig.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol

from .binding import DIGEST


class DeployMismatch(RuntimeError):
    """The cluster did not end up running the approved digest. The caller enters SAFE_HOLD."""


class Kube(Protocol):
    def current_image(self, namespace: str, deployment: str, container: str) -> str: ...
    def patch_image(self, namespace: str, deployment: str, patch: dict) -> None: ...
    def rollout_status(self, namespace: str, deployment: str, timeout_seconds: int) -> bool: ...
    def running_digests(self, namespace: str, deployment: str, container: str) -> list[str]: ...


def image_patch(repository: str, digest: str, container: str) -> dict:
    """Strategic-merge patch that pins exactly ``repository@sha256:…``. Tags are never used."""
    if not DIGEST.fullmatch(digest):
        raise ValueError("image digest format")
    if "@" in repository or ":" in repository.rsplit("/", 1)[-1]:
        raise ValueError("repository must not carry a tag or digest")
    return {"spec": {"template": {"spec": {"containers": [{"name": container, "image": f"{repository}@{digest}"}]}}}}


def apply_pinned_image(kube: Kube, *, namespace: str, deployment: str, container: str, repository: str,
                       digest: str, timeout_seconds: int = 300) -> dict:
    """Patch, wait, then verify every running pod reports the approved digest."""
    before = kube.current_image(namespace, deployment, container)
    kube.patch_image(namespace, deployment, image_patch(repository, digest, container))
    if not kube.rollout_status(namespace, deployment, timeout_seconds):
        raise DeployMismatch("ROLLOUT_NOT_COMPLETE")
    running = kube.running_digests(namespace, deployment, container)
    if not running or any(d != digest for d in running):
        raise DeployMismatch("RUNNING_DIGEST_MISMATCH")
    return {"previous_image": before, "deployed_digest": digest, "pods": len(running)}


class FakeKube:
    """In-memory cluster for tests and dry runs. ``rollout_result`` and ``drift`` model failures."""

    def __init__(self, image: str, *, rollout_result: bool = True, drift: str | None = None):
        self.image, self.rollout_result, self.drift = image, rollout_result, drift
        self.patches: list[dict] = []

    def current_image(self, namespace, deployment, container):
        return self.image

    def patch_image(self, namespace, deployment, patch):
        self.patches.append(patch)
        self.image = patch["spec"]["template"]["spec"]["containers"][0]["image"]

    def rollout_status(self, namespace, deployment, timeout_seconds):
        return self.rollout_result

    def running_digests(self, namespace, deployment, container):
        digest = self.drift or self.image.split("@", 1)[-1]
        return [digest, digest]


class KubectlKube:
    """Real adapter. ``kubeconfig`` must be the namespace-scoped apply identity (no cluster admin)."""

    def __init__(self, kubeconfig: str):
        self.kubeconfig = kubeconfig

    def _run(self, *args: str) -> str:
        return subprocess.run(["kubectl", "--kubeconfig", self.kubeconfig, *args], check=True,
                              capture_output=True, text=True, timeout=120).stdout

    def _deployment(self, namespace: str, deployment: str) -> dict[str, Any]:
        return json.loads(self._run("-n", namespace, "get", "deployment", deployment, "-o", "json"))

    def current_image(self, namespace, deployment, container):
        spec = self._deployment(namespace, deployment)["spec"]["template"]["spec"]
        return next(c["image"] for c in spec["containers"] if c["name"] == container)

    def patch_image(self, namespace, deployment, patch):
        self._run("-n", namespace, "patch", "deployment", deployment, "--type", "strategic", "-p", json.dumps(patch))

    def rollout_status(self, namespace, deployment, timeout_seconds):
        try:
            self._run("-n", namespace, "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout_seconds}s")
            return True
        except subprocess.CalledProcessError:
            return False

    def running_digests(self, namespace, deployment, container):
        selector = ",".join(f"{k}={v}" for k, v in self._deployment(namespace, deployment)["spec"]["selector"]["matchLabels"].items())
        pods = json.loads(self._run("-n", namespace, "get", "pods", "-l", selector, "-o", "json"))["items"]
        out = []
        for pod in pods:
            if pod.get("status", {}).get("phase") != "Running":
                continue
            for status in pod.get("status", {}).get("containerStatuses", []):
                if status.get("name") == container:
                    image_id = status.get("imageID", "")
                    out.append(image_id.split("@", 1)[-1] if "@" in image_id else image_id)
        return out
