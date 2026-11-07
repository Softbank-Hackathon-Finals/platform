"""Connect a bucket task to the existing T11 coordinator.

T11 owns snapshot validation, proposal schema and policy checks. This adapter
only prepares a bounded request and returns a proposal reference. It never
executes a proposal, supplies runtime credentials, or judges verification.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from .bucket_task import BucketPolicyTask, prepare_bucket_policy_task
from .runner import ProposalRef


class Snapshot(Protocol):
    def read(self, path: str) -> str: ...


class Coordinator(Protocol):
    bundle: object
    snapshot: Snapshot
    state: str
    attempt: int

    async def propose(self, provider: object) -> dict: ...


def validate_coordinator_attempt(coordinator: Coordinator, *, run_id: str, source_sha: str,
                                 attempt: int, feedback_digest: str | None, initial_summary: str) -> None:
    """Bind a T11 retry to independently authenticated failure feedback.

    The host must establish feedback authenticity before constructing this
    adapter; equality here checks identity, not the origin of the digest.
    """
    if type(attempt) is not int or not 1 <= attempt <= 3:
        raise ValueError("invalid proposal attempt")
    bundle = coordinator.bundle
    if (getattr(bundle, "run_id", None) != run_id or getattr(bundle, "source_sha", None) != source_sha
            or type(coordinator.attempt) is not int or coordinator.attempt != attempt - 1):
        raise ValueError("task and coordinator mismatch")
    if attempt == 1:
        if (coordinator.state != "READY" or feedback_digest is not None
                or getattr(bundle, "summary", None) != initial_summary):
            raise ValueError("invalid initial proposal context")
    elif (coordinator.state != "RETRY_ALLOWED" or not isinstance(feedback_digest, str)
          or re.fullmatch(r"[a-f0-9]{64}", feedback_digest) is None
          or getattr(bundle, "evidence_digest", None) != feedback_digest):
        raise ValueError("retry feedback mismatch")


def attempt_tool_events(coordinator: Coordinator, attempt: int) -> tuple[dict, ...]:
    """Return T11's audit events for one attempt, complete and in their original order.

    T11 records only reasons and categories, never raw commands or model
    arguments. An incomplete or truncated audit is unavailable, not filtered.
    """
    records = [a for a in getattr(coordinator, "attempts", []) if a.get("attempt") == attempt]
    if len(records) != 1 or records[0].get("audit_truncated") is not False:
        raise ValueError("tool events unavailable")
    events = tuple(dict(e) for e in coordinator.audit
                   if e.get("attempt") == attempt and e.get("tool") != "independent_verifier")
    if len(events) != records[0].get("audit_event_count"):
        raise ValueError("tool events incomplete")
    return events


# How the source policy and the target store are named to the model (ADR-0034). The model
# proposes the target policy; the deterministic export + render (`task_translation`) is the
# reference the rehearsal control is built from. aws → onprem keeps the original wording.
SOURCE_POLICY_NAMES = {"aws": "S3 policy", "gcp": "GCS IAM policy", "onprem": "SeaweedFS s3.json identities"}
TARGET_STORE_NAMES = {"onprem": "SeaweedFS", "aws": "S3", "gcp": "GCS"}


def bucket_summary(task: BucketPolicyTask, source_path: str, target_path: str) -> str:
    """Build the T11 FailureBundle summary from trusted task metadata."""
    return (
        f"Translate the {SOURCE_POLICY_NAMES[task.source_kind]} in {source_path} for "
        f"{TARGET_STORE_NAMES[task.target_kind]} test bucket {task.target_bucket}; "
        f"propose changes only to {target_path}. "
        "Deny anonymous reads and destructive access. Return a bounded policy change proposal."
    )


@dataclass(frozen=True)
class T11BucketProposer:
    coordinator: Coordinator = field(repr=False)
    provider: object = field(repr=False)
    source_policy_path: str
    target_policy_path: str
    attempt: int = 1
    feedback_digest: str | None = None

    def propose(self, task: object) -> ProposalRef:
        if not isinstance(task, BucketPolicyTask):
            raise ValueError("bucket task required")
        expected = prepare_bucket_policy_task(
            task.run_id, task.source_policy, task.target_bucket, source_sha=task.source_sha,
            source_kind=task.source_kind, target_kind=task.target_kind,
        )
        if task != expected:
            raise ValueError("invalid bucket task")
        coordinator = self.coordinator
        validate_coordinator_attempt(
            coordinator, run_id=task.run_id, source_sha=task.source_sha, attempt=self.attempt,
            feedback_digest=self.feedback_digest,
            initial_summary=bucket_summary(task, self.source_policy_path, self.target_policy_path),
        )
        if self.source_policy_path == self.target_policy_path:
            raise ValueError("source and target policy must be separate")
        # T11 Snapshot.read performs its own path, file, hash and secret checks.
        source = coordinator.snapshot.read(self.source_policy_path)
        coordinator.snapshot.read(self.target_policy_path)
        if (source != task.source_policy
                or hashlib.sha256(source.encode("utf-8")).hexdigest() != task.source_sha256):
            raise ValueError("source policy mismatch")
        # Avoid creating a coroutine that cannot run when embedded in an async host.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("synchronous proposer cannot run inside an event loop")
        result = asyncio.run(coordinator.propose(self.provider))
        if (result.get("state") != "PROPOSAL_READY" or type(result.get("attempt")) is not int
                or result.get("attempt") != self.attempt
                or result.get("run_id") != task.run_id or result.get("source_sha") != task.source_sha
                or not result.get("proposal") or not result.get("proposal_digest")):
            raise ValueError("T11 proposal unavailable")
        edits = result["proposal"].get("edits") if isinstance(result["proposal"], dict) else None
        if not isinstance(edits, list) or len(edits) != 1 or not isinstance(edits[0], dict) or edits[0].get("path") != self.target_policy_path:
            raise ValueError("proposal outside target policy")
        return ProposalRef(run_id=task.run_id, source_sha=task.source_sha,
                           proposal_digest=result["proposal_digest"])

    def tool_events(self) -> tuple[dict, ...]:
        if getattr(self.provider, "tool_events_available", True) is not True:
            raise ValueError("the model's tool events are kept by the remote runtime")
        return attempt_tool_events(self.coordinator, self.attempt)
