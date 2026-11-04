"""Validate credential proposal base content before preparing candidate files.

Snapshot access is delegated to T11's existing policy. This module neither reads
files itself nor writes a checkout, builds an image, or grants approval.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from .credential_proposal_source import ProposedEdit
from .credential_proposer import SOURCE_PATHS


class Snapshot(Protocol):
    def read(self, path: str) -> str: ...


@dataclass(frozen=True)
class CandidateFile:
    path: str
    content: str = field(repr=False)
    sha256: str


def prepare_candidate_files(snapshot: Snapshot, edits: tuple[ProposedEdit, ...]) -> tuple[CandidateFile, ...]:
    """Return replacement files only after each base matches the snapshot.

    The returned files are still untrusted. The supplied snapshot must implement
    T11's actual source policy; no path fallback or direct disk access is used.
    """
    if not isinstance(edits, tuple) or not 1 <= len(edits) <= len(SOURCE_PATHS):
        raise ValueError("invalid edits")
    if any(not isinstance(edit, ProposedEdit) or edit.path not in SOURCE_PATHS for edit in edits):
        raise ValueError("invalid edit")
    paths = [edit.path for edit in edits]
    if "app/db_secret.py" not in paths or len(paths) != len(set(paths)):
        raise ValueError("invalid edit scope")
    files = []
    for edit in edits:
        source = snapshot.read(edit.path)
        if not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != edit.before_sha256:
            raise ValueError("snapshot base mismatch")
        if not isinstance(edit.replacement, str) or len(edit.replacement.encode()) > 32768:
            raise ValueError("invalid replacement")
        files.append(CandidateFile(edit.path, edit.replacement, hashlib.sha256(edit.replacement.encode()).hexdigest()))
    return tuple(files)
