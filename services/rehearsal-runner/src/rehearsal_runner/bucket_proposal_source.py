"""Resolve a T11 bucket proposal by its immutable digest.

This is a local handoff boundary, not an authorization to apply a policy or
proof of T38 verification. The source must be controlled by the caller.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from .bucket_verifier import BucketVerificationRequest


class ProposalSource(Protocol):
    def result(self) -> dict: ...


@dataclass(frozen=True)
class BucketProposal:
    proposal_digest: str
    source_policy_sha256: str
    target_bucket: str
    replacement: str


@dataclass(frozen=True)
class BoundBucketProposalSource:
    source: ProposalSource
    target_policy_path: str

    def resolve(self, request: BucketVerificationRequest) -> BucketProposal:
        if not isinstance(request, BucketVerificationRequest):
            raise ValueError("invalid verification request")
        result = self.source.result()
        if not isinstance(result, dict):
            raise ValueError("invalid proposal result")
        proposal = result.get("proposal")
        if (result.get("state") != "PROPOSAL_READY" or type(result.get("attempt")) is not int
                or type(request.attempt) is not int or not 1 <= request.attempt <= 3
                or result["attempt"] != request.attempt or result.get("run_id") != request.run_id
                or result.get("source_sha") != request.source_sha
                or result.get("proposal_digest") != request.proposal_digest
                or not isinstance(proposal, dict)):
            raise ValueError("proposal identity mismatch")
        try:
            raw = json.dumps(proposal, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            raise ValueError("invalid proposal") from None
        if hashlib.sha256(raw).hexdigest() != request.proposal_digest:
            raise ValueError("proposal digest mismatch")
        edits = proposal.get("edits")
        if (proposal.get("run_id") != request.run_id or proposal.get("source_sha") != request.source_sha
                or not isinstance(edits, list) or len(edits) != 1 or not isinstance(edits[0], dict)
                or edits[0].get("path") != self.target_policy_path
                or not isinstance(edits[0].get("replacement"), str)):
            raise ValueError("proposal outside target policy")
        return BucketProposal(request.proposal_digest, request.source_policy_sha256,
                              request.target_bucket, edits[0]["replacement"])
