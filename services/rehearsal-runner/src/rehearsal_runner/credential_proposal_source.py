"""Resolve T11 demo-chat proposal edits by immutable proposal identity.

The returned edits are untrusted source text, not an authorization, verified
image or proof that a candidate was built from these edits.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from .credential_proposer import SOURCE_PATHS
from .credential_verifier import CredentialVerificationRequest


class ProposalSource(Protocol):
    def result(self) -> dict: ...


@dataclass(frozen=True)
class ProposedEdit:
    path: str
    before_sha256: str
    replacement: str = field(repr=False)


@dataclass(frozen=True)
class BoundCredentialProposalSource:
    source: ProposalSource = field(repr=False)

    def resolve(self, request: CredentialVerificationRequest) -> tuple[ProposedEdit, ...]:
        if not isinstance(request, CredentialVerificationRequest):
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
            encoded = json.dumps(proposal, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            raise ValueError("invalid proposal") from None
        if hashlib.sha256(encoded).hexdigest() != request.proposal_digest:
            raise ValueError("proposal digest mismatch")
        edits = proposal.get("edits")
        if (proposal.get("run_id") != request.run_id or proposal.get("source_sha") != request.source_sha
                or not isinstance(edits, list) or not 1 <= len(edits) <= len(SOURCE_PATHS)):
            raise ValueError("invalid credential proposal")
        out = []
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("invalid edit")
            path, before, replacement = edit.get("path"), edit.get("before_sha256"), edit.get("replacement")
            if (not isinstance(path, str) or path not in SOURCE_PATHS
                    or not isinstance(before, str) or re.fullmatch(r"[a-f0-9]{64}", before) is None
                    or not isinstance(replacement, str) or len(replacement.encode()) > 32768):
                raise ValueError("invalid edit")
            out.append(ProposedEdit(path, before, replacement))
        paths = [edit.path for edit in out]
        if "app/db_secret.py" not in paths or len(paths) != len(set(paths)):
            raise ValueError("proposal outside credential source scope")
        return tuple(out)
