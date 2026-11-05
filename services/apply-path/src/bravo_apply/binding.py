"""Check that what we are about to deploy is exactly what a person approved (design FR-7).

The T10 claim receipt binds ``diff_hash`` · ``image_digest`` · ``target_environment_id`` ·
``expires_at`` to one ``application_id``. T31 recomputes the diff hash from the proposal it was
handed, refuses anything that differs, and never widens the change beyond the approved files.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from datetime import UTC, datetime

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class BindingError(ValueError):
    """The claim, the proposal and the target do not describe the same approved change."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _lines(text: str) -> list[str]:
    """Split only on LF, as Git does; preserve CR and missing final newline. (T15 verbatim)"""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def file_diff(path: str, before: str, after: str) -> bytes:
    """Git-applicable UTF-8 unified diff for one file. Byte-for-byte the function the rehearsal
    runner (T15 ``credential_candidate.file_diff``) hashes, so the approval binding and this
    recomputation agree without sharing a package."""
    chunks = []
    for line in difflib.unified_diff(_lines(before), _lines(after), fromfile="a/" + path, tofile="b/" + path):
        chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(chunks).encode("utf-8")


def render_diff(sources: dict[str, str], edits: list[dict]) -> bytes:
    """The candidate diff T15 built: each edit's file diff, sorted by path, concatenated."""
    return b"".join(file_diff(e["path"], sources[e["path"]], e["replacement"]) for e in sorted(edits, key=lambda e: e["path"]))


def diff_hash(diff: bytes) -> str:
    return "sha256:" + hashlib.sha256(diff).hexdigest()


def diff_paths(diff: bytes) -> list[str]:
    """Files a unified diff touches, from its ``+++ b/<path>`` headers."""
    return sorted({line[6:].rstrip("\r") for line in diff.decode("utf-8", errors="replace").splitlines() if line.startswith("+++ b/")})


def _rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def verify_claim(claim: dict, proposal: dict, *, target_environment_id: str, now: datetime,
                 sources: dict[str, str] | None = None, diff: bytes | None = None) -> dict:
    """Return the verified binding or raise ``BindingError`` with a code-owned reason.

    ``diff`` is the candidate bytes T15 handed off; ``sources`` are the snapshot contents of the
    approved files so the diff can be recomputed from the proposal. One of the two is required,
    and when both are given they must agree."""
    for key in ("application_id", "approval_id", "verification_id", "binding", "claimed_at"):
        if key not in claim:
            raise BindingError("CLAIM_INCOMPLETE")
    for key in ("application_id", "approval_id", "verification_id"):
        if not IDENTIFIER.fullmatch(str(claim[key])):
            raise BindingError("CLAIM_IDENTIFIER_INVALID")
    binding = claim["binding"]
    required = {"plan_id", "plan_digest", "diff_hash", "image_digest", "target_environment_id", "expires_at"}
    if not isinstance(binding, dict) or not required <= set(binding):
        raise BindingError("BINDING_INCOMPLETE")
    if not DIGEST.fullmatch(binding["diff_hash"]) or not DIGEST.fullmatch(binding["image_digest"]):
        raise BindingError("BINDING_DIGEST_FORMAT")
    if binding["target_environment_id"] != target_environment_id:
        raise BindingError("TARGET_ENVIRONMENT_MISMATCH")
    if _rfc3339(binding["expires_at"]) <= now:
        raise BindingError("APPROVAL_EXPIRED")
    edits = proposal.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > 4 or len({e.get("path") for e in edits}) != len(edits):
        raise BindingError("PROPOSAL_SHAPE")
    if proposal.get("run_id") and claim.get("rehearsal_id") and proposal["run_id"] != claim["rehearsal_id"]:
        raise BindingError("PROPOSAL_RUN_MISMATCH")
    if sources is None and diff is None:
        raise BindingError("DIFF_SOURCE_REQUIRED")
    if sources is not None:
        for e in edits:
            if e["path"] not in sources:
                raise BindingError("SOURCE_MISSING")
            if hashlib.sha256(sources[e["path"]].encode("utf-8")).hexdigest() != e.get("before_sha256"):
                raise BindingError("BASE_DIGEST_MISMATCH")
        rendered = render_diff(sources, edits)
        if diff is not None and diff != rendered:
            raise BindingError("DIFF_BYTES_MISMATCH")
        diff = rendered
    if not diff:
        raise BindingError("DIFF_EMPTY")
    if diff_hash(diff) != binding["diff_hash"]:
        raise BindingError("DIFF_HASH_MISMATCH")
    approved = sorted({e["path"] for e in edits})
    if diff_paths(diff) != approved:
        raise BindingError("DIFF_OUTSIDE_APPROVED_PATHS")
    return {"plan_id": binding["plan_id"], "diff_hash": binding["diff_hash"], "image_digest": binding["image_digest"],
            "target_environment_id": binding["target_environment_id"], "expires_at": binding["expires_at"],
            "approved_paths": approved}
