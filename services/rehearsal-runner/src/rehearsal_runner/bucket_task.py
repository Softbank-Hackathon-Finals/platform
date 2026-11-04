"""Prepare a bounded bucket-policy translation task; never apply policy."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BucketPolicyTask:
    run_id: str
    source_sha256: str
    source_policy: str
    target_bucket: str
    target_service: Literal["seaweedfs"] = "seaweedfs"

    def as_model_input(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
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


def prepare_bucket_policy_task(run_id: str, source_policy: str, target_bucket: str) -> BucketPolicyTask:
    """Capture source policy without treating a generated proposal as verified."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("invalid run ID")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", target_bucket):
        raise ValueError("invalid test bucket")
    if not isinstance(source_policy, str) or len(source_policy.encode("utf-8")) > 32768:
        raise ValueError("invalid source policy")
    try:
        policy = json.loads(source_policy, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError):
        raise ValueError("invalid source policy") from None
    if not isinstance(policy, dict) or not isinstance(policy.get("Statement"), (list, dict)):
        raise ValueError("source policy requires statements")
    return BucketPolicyTask(run_id, hashlib.sha256(source_policy.encode("utf-8")).hexdigest(),
                            source_policy, target_bucket)
