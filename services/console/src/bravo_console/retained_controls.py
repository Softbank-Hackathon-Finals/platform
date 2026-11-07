"""Explicit local T14 history for preparation, never current T19 authority.

A pinned digest identifies preserved bytes; it does not authenticate a producer,
plan, current resource or gate decision. No uploads or user-selected paths.
"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .reports import CATALOG, SCHEMA
from .wire import object_document
from .workload import identifier

VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/ControlRun", "$defs": SCHEMA["$defs"]},
    format_checker=FormatChecker(),
)


@dataclass(frozen=True)
class RetainedRun:
    tenant_id: str
    environment_id: str
    lane: str
    environment_ref: str
    application_id: str
    profile_id: str
    profile_version: str
    path: Path
    sha256: str

    def __post_init__(self):
        identifier(self.tenant_id)
        identifier(self.environment_id)
        identifier(self.application_id)
        if (
            self.lane not in {"source", "target"}
            or type(self.environment_ref) is not str
            or not self.environment_ref
            or not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or not re.fullmatch("[a-f0-9]{64}", self.sha256)
        ):
            raise ValueError("explicit_retained_run_required")
        if (
            len(
                [
                    p
                    for p in CATALOG["profiles"]
                    if (p["id"], p["version"]) == (self.profile_id, self.profile_version)
                ]
            )
            != 1
        ):
            raise ValueError("registered_retained_profile_required")

    def read(self, now):
        base = dict(
            environment_id=self.environment_id,
            lane=self.lane,
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            authorizes_execution=False,
            current_resource_verified=False,
        )
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                mode = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(mode.st_mode)
                    or mode.st_mode & 0o077
                    or mode.st_uid != os.getuid()
                    or mode.st_size > 262144
                ):
                    raise ValueError("protected_retained_run_required")
                raw = stream.read(262145)
            if hashlib.sha256(raw).hexdigest() != self.sha256:
                raise ValueError("retained_run_changed")
            run = object_document(raw, max_bytes=262144)
            VALIDATOR.validate(run)
            if run["app"] != self.application_id or run["environment"] != dict(
                id=self.environment_id, role=self.lane, ref=self.environment_ref
            ):
                raise ValueError("retained_run_scope_mismatch")
            profile = next(
                p
                for p in CATALOG["profiles"]
                if (p["id"], p["version"]) == (self.profile_id, self.profile_version)
            )
            required = profile["required_test_ids"]
            rows = run["results"]
            if (
                len(rows) != len(required)
                or sorted(r["test_id"] for r in rows) != sorted(required)
                or any(
                    r["control_id"] != CATALOG["tests"][r["test_id"]]["control_id"] for r in rows
                )
            ):
                raise ValueError("incomplete_retained_run")
            start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
            finish = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
            if not start <= finish <= now:
                raise ValueError("invalid_retained_run_time")
            projected = []
            for row in rows:
                proof = row["evidence"]
                if proof is None:
                    evidence_state = "NO_EVIDENCE"
                else:
                    collected = datetime.fromisoformat(proof["collected_at"].replace("Z", "+00:00"))
                    expires = datetime.fromisoformat(proof["expires_at"].replace("Z", "+00:00"))
                    if not start <= collected <= finish or not collected < expires:
                        raise ValueError("invalid_retained_evidence_time")
                    evidence_state = "EXPIRED" if now >= expires else "RECORDED"
                projected.append(
                    dict(
                        test_id=row["test_id"],
                        control_id=row["control_id"],
                        recorded_verdict=row["verdict"],
                        evidence_state=evidence_state,
                        unknown_reason=row["unknown_reason"],
                    )
                )
            return base | dict(
                status="HISTORY",
                finished_at=run["finished_at"],
                document_sha256=self.sha256,
                rows=projected,
            )
        except Exception:
            return base | dict(
                status="UNAVAILABLE", finished_at=None, document_sha256=None, rows=[]
            )
