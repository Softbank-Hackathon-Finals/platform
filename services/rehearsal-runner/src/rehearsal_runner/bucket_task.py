"""Prepare a bounded bucket-policy translation task; never apply policy.

The source and target kinds (ADR-0034) choose the exporter and renderer. The default pair
aws → onprem keeps the original task fields byte for byte, so existing task digests hold.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from .neutral.export import OBJECT_EXPORTERS
from .neutral.models import KINDS, NeutralError
from .neutral.registry import SOURCE_EXPORT, Unsupported

# What the target stores bucket policies in, as named to the model.
TARGET_SERVICES = {"onprem": "seaweedfs", "aws": "s3", "gcp": "gcs"}


@dataclass(frozen=True)
class BucketPolicyTask:
    run_id: str
    source_sha: str
    source_sha256: str
    source_policy: str = field(repr=False)
    target_bucket: str
    target_service: Literal["seaweedfs", "s3", "gcs"] = "seaweedfs"
    source_kind: Literal["aws", "gcp", "onprem"] = "aws"
    target_kind: Literal["aws", "gcp", "onprem"] = "onprem"

    def __post_init__(self):
        if (self.source_kind not in KINDS or self.target_kind not in KINDS
                or TARGET_SERVICES[self.target_kind] != self.target_service):
            raise ValueError("invalid environment kinds")

    def as_model_input(self) -> dict[str, str]:
        kinds = {} if (self.source_kind, self.target_kind) == ("aws", "onprem") else {
            "source_kind": self.source_kind, "target_kind": self.target_kind}
        return kinds | {
            "run_id": self.run_id,
            "source_sha": self.source_sha,
            "source_sha256": self.source_sha256,
            "source_policy": self.source_policy,
            "target_service": self.target_service,
            "target_bucket": self.target_bucket,
            "objective": "Translate the policy without granting anonymous read or destructive access.",
        }


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate policy key")
        obj[key] = value
    return obj


def prepare_bucket_policy_task(
    run_id: str, source_policy: str, target_bucket: str, *, source_sha: str,
    source_kind: str = "aws", target_kind: str = "onprem",
) -> BucketPolicyTask:
    """Capture source policy without treating a generated proposal as verified."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("invalid source revision")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", target_bucket):
        raise ValueError("invalid test bucket")
    if not isinstance(source_policy, str) or len(source_policy.encode("utf-8")) > 32768:
        raise ValueError("invalid source policy")
    try:
        policy = json.loads(source_policy, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise ValueError("invalid source policy") from None
    if source_kind not in OBJECT_EXPORTERS:
        raise Unsupported(SOURCE_EXPORT, str(source_kind))
    if target_kind not in TARGET_SERVICES:
        raise ValueError("invalid environment kinds")
    if source_kind == "aws":
        if not isinstance(policy, dict) or not isinstance(policy.get("Statement"), (list, dict)):
            raise ValueError("source policy requires statements")
    else:
        # The exporter is the parser for every other source kind; untranslatable is refused here.
        try:
            # A GCS policy names no bucket; its grants apply to whichever bucket holds it.
            OBJECT_EXPORTERS[source_kind](source_policy, bucket=target_bucket if source_kind == "gcp" else None)
        except NeutralError:
            raise ValueError("invalid source policy") from None
    return BucketPolicyTask(run_id, source_sha, hashlib.sha256(source_policy.encode("utf-8")).hexdigest(),
                            source_policy, target_bucket, TARGET_SERVICES[target_kind], source_kind, target_kind)
