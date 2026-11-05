"""Locate a live T15 namespace by rehearsal ID and attempt, read-only.

Labels identify candidates, not authenticated verification or cleanup evidence.
A missing namespace never proves that creation did not occur or that a specific
UID was removed. T10 must receive independently authenticated T38 observations.
"""

import hashlib
import re
from dataclasses import dataclass

from .isolation.kube import Kubectl

RUN_LABEL = "bravo.platform/run-hash"
ATTEMPT_LABEL = "bravo.platform/attempt"
RUN_ANNOTATION = "bravo.platform/rehearsal-id"


class RecoveryUnavailable(RuntimeError):
    """The lookup could not establish one unambiguous live candidate."""


@dataclass(frozen=True)
class LiveNamespaceCandidate:
    name: str
    uid: str


def recovery_identity(run_id: str, attempt: int) -> tuple[dict[str, str], dict[str, str]]:
    if (not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id) is None
            or type(attempt) is not int or not 1 <= attempt <= 3):
        raise ValueError("invalid rehearsal identity")
    # The full unhashed identifier is independently checked in the annotation.
    labels = {RUN_LABEL: hashlib.sha256(run_id.encode("ascii")).hexdigest()[:32], ATTEMPT_LABEL: str(attempt)}
    return labels, {RUN_ANNOTATION: run_id}


def find_live_namespace(kube: Kubectl, run_id: str, attempt: int) -> LiveNamespaceCandidate | None:
    """Locate one live namespace; None means only that no candidate was found."""
    labels, annotations = recovery_identity(run_id, attempt)
    selector = ",".join(f"{key}={value}" for key, value in labels.items())
    try:
        response = kube.get_json("namespaces", "-l", selector)
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list) or len(items) > 1:
            raise RecoveryUnavailable("ambiguous namespace lookup")
        if not items:
            return None
        meta = items[0]["metadata"]
        actual_labels, actual_annotations = meta["labels"], meta["annotations"]
        name, uid = meta["name"], meta["uid"]
        if (not isinstance(actual_labels, dict) or not isinstance(actual_annotations, dict)
                or any(actual_labels.get(k) != v for k, v in labels.items())
                or any(actual_annotations.get(k) != v for k, v in annotations.items())
                or actual_labels.get("app.kubernetes.io/part-of") != "bravo-rehearsal"
                or not isinstance(name, str) or re.fullmatch(r"rehearsal-[a-f0-9]{24}", name) is None
                or not isinstance(uid, str) or not uid.strip() or meta.get("deletionTimestamp")):
            raise RecoveryUnavailable("namespace identity mismatch")
        return LiveNamespaceCandidate(name, uid)
    except RecoveryUnavailable:
        raise
    except Exception:
        raise RecoveryUnavailable("namespace lookup unavailable") from None


@dataclass(frozen=True)
class LiveJobCandidate:
    name: str
    uid: str


def find_live_jobs(
    kube: Kubectl, namespace: LiveNamespaceCandidate, run_id: str, attempt: int,
) -> tuple[LiveJobCandidate, ...]:
    """List labelled live Jobs without claiming their identity is authentic.

    A namespace UID mismatch before or after the listing invalidates every
    candidate. The trusted verifier must independently bind Job UID, Pod UID,
    image, logs and its authenticated result before T10 may reconcile it.
    """
    labels, annotations = recovery_identity(run_id, attempt)
    if (not isinstance(namespace, LiveNamespaceCandidate)
            or not isinstance(namespace.name, str)
            or re.fullmatch(r"rehearsal-[a-f0-9]{24}", namespace.name) is None
            or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
        raise ValueError("invalid namespace candidate")
    selector = ",".join(f"{key}={value}" for key, value in labels.items())

    def check_namespace() -> None:
        value = kube.get_json("namespace", namespace.name)
        metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
        if (metadata.get("uid") != namespace.uid or metadata.get("deletionTimestamp")
                or not isinstance(metadata.get("labels"), dict)
                or not isinstance(metadata.get("annotations"), dict)
                or metadata["labels"].get("app.kubernetes.io/part-of") != "bravo-rehearsal"
                or any(metadata["labels"].get(key) != val for key, val in labels.items())
                or any(metadata["annotations"].get(key) != val for key, val in annotations.items())):
            raise RecoveryUnavailable("namespace identity changed")

    try:
        check_namespace()
        result = kube.get_json("jobs", "-n", namespace.name, "-l", selector)
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) > 32:
            raise RecoveryUnavailable("ambiguous job lookup")
        jobs = []
        for item in items:
            meta = item["metadata"]
            name, uid = meta["name"], meta["uid"]
            actual_labels, actual_annotations = meta["labels"], meta["annotations"]
            if (not isinstance(name, str) or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name) is None
                    or not isinstance(uid, str) or not uid.strip() or meta.get("deletionTimestamp")
                    or meta.get("namespace") != namespace.name
                    or not isinstance(actual_labels, dict) or not isinstance(actual_annotations, dict)
                    or any(actual_labels.get(k) != v for k, v in labels.items())
                    or any(actual_annotations.get(k) != v for k, v in annotations.items())):
                raise RecoveryUnavailable("job identity mismatch")
            jobs.append(LiveJobCandidate(name, uid))
        if len({job.uid for job in jobs}) != len(jobs) or len({job.name for job in jobs}) != len(jobs):
            raise RecoveryUnavailable("duplicate job identity")
        check_namespace()
        return tuple(sorted(jobs, key=lambda job: job.name))
    except RecoveryUnavailable:
        raise
    except Exception:
        raise RecoveryUnavailable("job lookup unavailable") from None
