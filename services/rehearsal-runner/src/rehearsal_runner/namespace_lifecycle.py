"""T15 namespace creation; deletion delegates to the existing UID-aware cleaner.

Requires caller-held T10 execution authority. Not a production entry point: the
T38 verifier must first support the same namespace UID and isolation evidence.
"""

import json
import re
import secrets
from dataclasses import dataclass
from typing import Protocol

from . import namespace as templates
from .isolation.kube import Kubectl
from .recovery_lookup import recovery_identity
from .runner import Namespace


class UIDCleaner(Protocol):
    def cleanup(self, namespace: str, uid: str) -> bool:
        """Delete only the original UID and confirm absence."""
        ...


class LifecycleUnavailable(RuntimeError):
    """A namespace operation or its result could not be confirmed."""


@dataclass(frozen=True)
class NamespaceLifecycle:
    kube: Kubectl
    cleaner: UIDCleaner
    profile: dict[str, str]
    attempt: int = 1

    def create(self, run_id: str) -> Namespace:
        labels, annotations = recovery_identity(run_id, self.attempt)
        # Random names avoid reusing an old run; create-only never adopts an existing namespace.
        name = "rehearsal-" + secrets.token_hex(12)
        documents = templates.render(name, self.profile).split("\n---\n")
        if len(documents) < 2:
            raise LifecycleUnavailable("namespace templates incomplete")
        anchor = "  labels:\n"
        if documents[0].count(anchor) != 1:
            raise LifecycleUnavailable("namespace metadata template invalid")
        additions = "  annotations:\n" + "".join(f'    {key}: "{value}"\n' for key, value in annotations.items())
        additions += anchor + "".join(f'    {key}: "{value}"\n' for key, value in labels.items())
        namespace_document = documents[0].replace(anchor, additions, 1)
        try:
            created = self.kube.run("create", "-f", "-", "-o", "json", stdin=namespace_document)
        except Exception:
            # A timeout can occur after the API accepted the request; do not guess its UID.
            raise LifecycleUnavailable("namespace creation unknown") from None
        if created.returncode != 0:
            raise LifecycleUnavailable("namespace creation not confirmed")
        try:
            metadata = json.loads(created.stdout)["metadata"]
            uid = metadata["uid"]
            if metadata["name"] != name or not isinstance(uid, str) or not uid.strip():
                raise ValueError("unexpected namespace identity")
        except (ValueError, TypeError, KeyError):
            raise LifecycleUnavailable("namespace identity unknown") from None
        namespace = Namespace(name, uid)
        try:
            self._require_uid(namespace, run_id)
            applied = self.kube.run("apply", "--server-side", "--field-manager=rehearsal-runner",
                                    "-f", "-", stdin="\n---\n".join(documents[1:]))
            if applied.returncode != 0:
                raise LifecycleUnavailable("namespace template application failed")
            self._require_uid(namespace, run_id)
        except Exception:
            # UID-precondition cleanup cannot delete a replacement namespace.
            try:
                self.cleanup(namespace)
            except Exception:
                pass
            raise LifecycleUnavailable("namespace setup not confirmed") from None
        return namespace

    def _require_uid(self, namespace: Namespace, run_id: str) -> None:
        metadata = self.kube.get_json("namespace", namespace.name).get("metadata", {})
        labels, annotations = recovery_identity(run_id, self.attempt)
        actual_labels, actual_annotations = metadata.get("labels", {}), metadata.get("annotations", {})
        if (metadata.get("uid") != namespace.uid or metadata.get("deletionTimestamp")
                or not isinstance(actual_labels, dict) or not isinstance(actual_annotations, dict)
                or actual_labels.get("app.kubernetes.io/part-of") != "bravo-rehearsal"
                or any(actual_labels.get(k) != v for k, v in labels.items())
                or any(actual_annotations.get(k) != v for k, v in annotations.items())):
            raise LifecycleUnavailable("namespace identity changed")

    def cleanup(self, namespace: Namespace) -> bool:
        # The injected cleaner is T38's UID-precondition implementation; no name-only delete.
        if (not isinstance(namespace, Namespace)
                or re.fullmatch(r"rehearsal-[a-f0-9]{24}", namespace.name) is None
                or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
            raise LifecycleUnavailable("invalid namespace identity")
        return self.cleaner.cleanup(namespace.name, namespace.uid) is True
