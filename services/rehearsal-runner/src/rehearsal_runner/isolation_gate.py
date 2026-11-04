"""Fail-closed T3 isolation gate for a caller-created rehearsal namespace.

This gate does not authorize a verification Job or approval. The caller must bind
its later operations and cleanup to the returned namespace UID.
"""

import re
import secrets
import time
from dataclasses import dataclass

from .isolation.__main__ import run
from .isolation.fixtures import RUN_LABEL, Fixtures
from .isolation.kube import Kubectl
from .isolation.model import EXIT_OK, Kind, exit_code
from .isolation.profile import Target


@dataclass(frozen=True)
class IsolationPass:
    namespace: str
    namespace_uid: str
    probe_run_id: str


class IsolationBlocked(RuntimeError):
    """Isolation or its cleanup could not be established."""


def _namespace_uid(kube: Kubectl, namespace: str) -> str:
    metadata = kube.get_json("namespace", namespace).get("metadata", {})
    uid = metadata.get("uid")
    labels = metadata.get("labels", {})
    if (not isinstance(uid, str) or not uid or metadata.get("deletionTimestamp")
            or not isinstance(labels, dict)
            or labels.get("app.kubernetes.io/part-of") != "bravo-rehearsal"):
        raise IsolationBlocked("namespace identity unavailable")
    return uid


def _fixtures_absent(kube: Kubectl, fx: Fixtures) -> bool:
    selector = f"{RUN_LABEL}={fx.run_id}"
    resources = kube.get_json("pod,service", "-n", fx.namespace, "-l", selector)
    victims = kube.get_json("namespaces", "--field-selector", f"metadata.name={fx.victim_namespace}")
    return resources.get("items") == [] and victims.get("items") == []


def check_isolation(
    kube: Kubectl, namespace: str, targets: list[Target], image: str, *,
    expected_uid: str | None = None, cleanup_timeout: float = 30
) -> IsolationPass:
    """Run T3 checks and return an identity only after confirmed fixture cleanup."""
    if not isinstance(namespace, str) or not re.fullmatch(
        r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?", namespace
    ):
        raise IsolationBlocked("namespace outside rehearsal scope")
    fx = Fixtures(namespace, secrets.token_hex(8), image)
    try:
        uid = _namespace_uid(kube, namespace)
        if expected_uid is not None and (not expected_uid.strip() or uid != expected_uid):
            raise IsolationBlocked("namespace identity does not match creation")
        try:
            results = run(kube, fx, targets)
        finally:
            fx.cleanup(kube)
        if not any(result.kind is Kind.ISOLATION for result in results) or exit_code(results) != EXIT_OK:
            raise IsolationBlocked("isolation test did not pass")
        if _namespace_uid(kube, namespace) != uid:
            raise IsolationBlocked("namespace identity changed")
        deadline = time.monotonic() + max(cleanup_timeout, 0)
        while not _fixtures_absent(kube, fx):
            if time.monotonic() >= deadline:
                raise IsolationBlocked("isolation fixtures remain")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        if _namespace_uid(kube, namespace) != uid:
            raise IsolationBlocked("namespace identity changed during cleanup")
        return IsolationPass(namespace, uid, fx.run_id)
    except IsolationBlocked:
        raise
    except Exception as exc:
        raise IsolationBlocked("isolation test or cleanup unavailable") from exc
