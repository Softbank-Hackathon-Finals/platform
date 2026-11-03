"""Immutable server bindings; constructing an object alone authenticates nothing."""

import re
from dataclasses import asdict, dataclass

from .contract import GateError, canonical, decode, fingerprint


def identity(value):
    if (
        type(value) is not str
        or not 0 < len(value) <= 512
        or value != value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise GateError("INVALID_IDENTITY")


def digest(value):
    if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):
        raise GateError("INVALID_DIGEST")


@dataclass(frozen=True)
class GateContext:
    tenant_id: str
    app: str
    gate_id: str
    plan_digest: str
    profile_id: str
    profile_version: str
    catalog_digest: str
    source_environment_id: str
    source_environment_ref: str
    target_environment_id: str
    target_environment_ref: str
    source_ref: str
    target_ref: str
    source_digest: str
    target_digest: str

    def __post_init__(self):
        for key, value in asdict(self).items():
            (digest if key.endswith("_digest") else identity)(value)
        if (
            self.source_environment_id == self.target_environment_id
            or self.source_environment_ref == self.target_environment_ref
        ):
            raise GateError("SOURCE_TARGET_ALIAS")

    @property
    def fingerprint(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class RunSnapshot:
    ref: str
    raw: bytes

    def __post_init__(self):
        identity(self.ref)
        decode(self.raw)

    @property
    def document(self):
        return decode(self.raw)

    @property
    def fingerprint(self):
        return fingerprint(self.document)


@dataclass(frozen=True)
class StoredException:
    body_json: str
    context_digest: str
    revision: int
    revoked: bool

    def __post_init__(self):
        digest(self.context_digest)
        if (
            type(self.revision) is not int
            or not 1 <= self.revision <= 2**63 - 1
            or type(self.revoked) is not bool
        ):
            raise GateError("INVALID_APPROVAL_RECORD")
        if type(self.body_json) is not str or len(self.body_json.encode("utf-8")) > 16384:
            raise GateError("BOUNDED_APPROVAL_RECORD_REQUIRED")
        body = decode(self.body_json.encode())
        object.__setattr__(self, "body_json", canonical(body).decode())

    @property
    def body(self):
        return decode(self.body_json.encode())

    @property
    def fingerprint(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class SecurityPrerequisite:
    context_digest: str
    report_digest: str
    valid_until: str
    exception_ids: tuple[str, ...]

    @property
    def authorizes_cutover(self):
        # T10 must independently check data readiness, operational approval,
        # execution ownership and current state immediately before dispatch.
        return False
