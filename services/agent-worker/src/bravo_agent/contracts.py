"""Typed boundary between T10/T15, the model, and the independent verifier."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .policy import check_policy_permissions

RunId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
SourceSha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

SECRET = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|sk-(?:ant-)?[A-Za-z0-9_-]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:\b(?:password|api[_-]?key|access[_-]?token|secret[_-]?key)\b"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{8,})"
)
DESTRUCTIVE = re.compile(
    r"(?i:\brm\s+-[^\n]*r[^\n]*f|\brm\s+-[^\n]*f[^\n]*r|\b(?:DROP|TRUNCATE)\b"
    r"|\bterraform\s+destroy\b|\bkubectl\s+delete\s+(?:namespace|ns)\b"
    r"|\b(?:delete[-_]?bucket|DeleteBucket)\b|\baws\s+s3\s+rb\b)"
)


def safe_text(value: str) -> str:
    if "\x00" in value or SECRET.search(value):
        raise ValueError("unsafe text")
    return value


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    blocked = {"test", "tests", "hidden-tests", "secrets", "credentials", "node_modules"}
    if (not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", value) or path.is_absolute()
            or str(path) != value or any(p.startswith(".") or p.lower() in blocked for p in path.parts)
            or any(token in path.name.lower() for token in ("credential", "secret", "private_key"))
            or re.search(r"(^test_|_test\.|\.test\.|\.spec\.)", path.name.lower())
            or path.suffix.lower() in {".pem", ".key", ".p12", ".tfstate"}):
        raise ValueError("path outside source policy")
    return value


def digest(value: BaseModel | dict) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SnapshotManifest(Contract):
    """Produced by the trusted source collector, never by model output."""

    source_sha: SourceSha
    files: dict[str, Digest] = Field(min_length=1, max_length=64)
    editable_paths: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def paths(self):
        for path in self.files:
            safe_path(path)
        if len(set(self.editable_paths)) != len(self.editable_paths):
            raise ValueError("duplicate editable path")
        if not set(self.editable_paths) <= self.files.keys():
            raise ValueError("editable paths must be in the snapshot")
        return self


class FailureBundle(Contract):
    run_id: RunId
    source_sha: SourceSha
    failure_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    summary: str = Field(min_length=1, max_length=4096)
    evidence_digest: Digest
    stage: Literal["REHEARSE"] = "REHEARSE"

    _summary = field_validator("summary")(safe_text)


class FileEdit(Contract):
    path: str
    before_sha256: Digest
    replacement: str = Field(max_length=32768)

    _path = field_validator("path")(safe_path)
    _replacement = field_validator("replacement")(safe_text)

    @field_validator("replacement")
    @classmethod
    def policy(cls, value):
        if DESTRUCTIVE.search(value):
            raise ValueError("destructive command")
        check_policy_permissions(value)
        return value


class Proposal(Contract):
    schema_version: Literal[1] = 1
    run_id: RunId
    source_sha: SourceSha
    rationale: str = Field(min_length=1, max_length=2048)
    risk: Literal["low", "medium", "high"]
    edits: list[FileEdit] = Field(min_length=1, max_length=4)

    _rationale = field_validator("rationale")(safe_text)

    @model_validator(mode="after")
    def unique_paths(self):
        if len({edit.path for edit in self.edits}) != len(self.edits):
            raise ValueError("duplicate edit")
        return self


class VerificationReceipt(Contract):
    """T15 supplies this through the control plane; it is never an agent tool."""

    run_id: RunId
    source_sha: SourceSha
    proposal_digest: Digest
    attempt: int = Field(ge=1, le=3)
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    effects_known: bool
    evidence_digest: Digest
    failure: FailureBundle | None = None

    @model_validator(mode="after")
    def failure_context(self):
        if (self.verdict == "FAIL") != (self.failure is not None):
            raise ValueError("only a failed verification supplies failure context")
        return self
