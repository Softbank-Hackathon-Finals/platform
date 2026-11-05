"""Validate credential proposal base content before preparing candidate files.

Snapshot access is delegated to T11's existing policy. This module neither reads
files itself nor writes a checkout, builds an image, or grants approval.
"""

import difflib
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


@dataclass(frozen=True)
class CandidateChange:
    files: tuple[CandidateFile, ...]
    diff: bytes = field(repr=False)
    diff_hash: str


def _checked_sources(snapshot: Snapshot, edits: tuple[ProposedEdit, ...]) -> list[tuple[str, CandidateFile]]:
    """Read each allowed source once and validate its proposed base digest."""
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
        if (not isinstance(source, str) or len(source.encode()) > 32768
                or hashlib.sha256(source.encode()).hexdigest() != edit.before_sha256):
            raise ValueError("snapshot base mismatch")
        if not isinstance(edit.replacement, str) or len(edit.replacement.encode()) > 32768:
            raise ValueError("invalid replacement")
        files.append((source, CandidateFile(edit.path, edit.replacement,
                                            hashlib.sha256(edit.replacement.encode()).hexdigest())))
    return files


def prepare_candidate_files(snapshot: Snapshot, edits: tuple[ProposedEdit, ...]) -> tuple[CandidateFile, ...]:
    """Return replacements only after each base matches the T11 snapshot."""
    return tuple(candidate for _, candidate in _checked_sources(snapshot, edits))


def _lines(text: str) -> list[str]:
    """Split only on LF, as Git does; preserve CR and missing final newline."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def prepare_candidate_change(snapshot: Snapshot, edits: tuple[ProposedEdit, ...]) -> CandidateChange:
    """Build an applicable UTF-8 diff from exactly the validated source bytes.

    Files and diff remain untrusted until independent verification. This does
    not establish that a built or running image contains these file contents.
    """
    checked = _checked_sources(snapshot, edits)
    chunks = []
    for source, candidate in sorted(checked, key=lambda item: item[1].path):
        for line in difflib.unified_diff(_lines(source), _lines(candidate.content),
                                         fromfile="a/" + candidate.path, tofile="b/" + candidate.path):
            if line.endswith("\n"):
                chunks.append(line)
            else:
                chunks.append(line + "\n\\ No newline at end of file\n")
    diff = "".join(chunks).encode("utf-8")
    if not diff:
        raise ValueError("candidate contains no changes")
    return CandidateChange(tuple(candidate for _, candidate in checked), diff,
                           "sha256:" + hashlib.sha256(diff).hexdigest())
