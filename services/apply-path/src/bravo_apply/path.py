"""The approved-change path, end to end, as one deterministic function (design 7.1 step 6)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path

import jsonschema

from .binding import BindingError, verify_claim
from .deploy import DeployMismatch, Kube, apply_pinned_image

# Controls the gate must ask again after the application changed how it reaches secrets and
# objects (docs/controls/README.md). The gate decides; T31 only requests the recheck.
GATE_RECHECK_TESTS = ["SEC-01.1", "IAM-01.1", "ENC-01.1"]


@dataclass
class ApplyOutcome:
    schema_version: str
    application_id: str
    approval_id: str
    verification_id: str
    plan_id: str
    outcome: str  # APPLIED | REFUSED | SAFE_HOLD
    reason: str
    change: dict
    deployed_digest: str | None
    job_passed: bool
    gate_recheck: dict | None
    steps: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    def t10_application(self) -> dict | None:
        """The exact fields T10 ``Application`` compares under the plan lock (workflow.py)."""
        if self.outcome != "APPLIED":
            return None
        return {"change": dict(self.change), "verification_id": self.verification_id, "approval_id": self.approval_id,
                "application_id": self.application_id, "job_passed": self.job_passed, "deployed_digest": self.deployed_digest}


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(resources.files("bravo_apply.schemas").joinpath("apply-result.schema.json").read_text("utf-8"))
    return jsonschema.Draft202012Validator(schema)


def validate_outcome(doc: dict) -> None:
    _validator().validate(doc)


def apply_approved_change(claim: dict, proposal: dict, *, kube: Kube, target_environment_id: str, namespace: str,
                          deployment: str, container: str, repository: str, sources: dict[str, str] | None = None,
                          diff: bytes | None = None, receipts_dir: Path | None = None,
                          now: datetime | None = None, timeout_seconds: int = 300) -> ApplyOutcome:
    now = now or datetime.now(UTC)
    base = {"schema_version": "1.0", "application_id": str(claim.get("application_id", "unknown")),
            "approval_id": str(claim.get("approval_id", "unknown")), "verification_id": str(claim.get("verification_id", "unknown")),
            "plan_id": str((claim.get("binding") or {}).get("plan_id", "unknown"))}
    steps: list[dict] = []

    def done(outcome, reason, change, deployed, job_passed, recheck):
        result = ApplyOutcome(**base, outcome=outcome, reason=reason, change=change, deployed_digest=deployed,
                              job_passed=job_passed, gate_recheck=recheck, steps=steps)
        validate_outcome(result.as_dict())
        if receipts_dir is not None:
            _write_receipt(receipts_dir, result)
        return result

    unknown_change = {"diff_hash": "sha256:" + "0" * 64, "image_digest": "sha256:" + "0" * 64}
    # 0. one application id is applied at most once; a second attempt is refused before any cluster call
    if receipts_dir is not None and (receipts_dir / f"{base['application_id']}.json").exists():
        steps.append({"step": "idempotency", "status": "refused", "detail": "receipt already exists for application_id"})
        return done("REFUSED", "ALREADY_APPLIED", unknown_change, None, False, None)
    # 1. binding: claim ↔ proposal ↔ target ↔ time
    try:
        binding = verify_claim(claim, proposal, target_environment_id=target_environment_id, now=now, sources=sources, diff=diff)
    except BindingError as exc:
        steps.append({"step": "binding", "status": "refused", "detail": exc.code})
        return done("REFUSED", exc.code, unknown_change, None, False, None)
    change = {"diff_hash": binding["diff_hash"], "image_digest": binding["image_digest"]}
    steps.append({"step": "binding", "status": "ok", "detail": f"{len(binding['approved_paths'])} approved path(s)"})
    # 2. the image is the rehearsal-verified digest; nothing is built here
    steps.append({"step": "image", "status": "ok", "detail": "promote rehearsal-verified digest, no rebuild"})
    # 3. deploy pinned to the digest and confirm what is running
    try:
        deployed = apply_pinned_image(kube, namespace=namespace, deployment=deployment, container=container,
                                      repository=repository, digest=binding["image_digest"], timeout_seconds=timeout_seconds)
    except DeployMismatch as exc:
        steps.append({"step": "deploy", "status": "failed", "detail": str(exc)})
        # Nothing is rolled back automatically: the previous digest is restored only after a person looks (design 8.2).
        return done("SAFE_HOLD", str(exc), change, None, False, None)
    steps.append({"step": "deploy", "status": "ok", "detail": f"{deployed['pods']} pod(s) on approved digest"})
    # 4. ask the gate to re-check the controls this change touches
    recheck = {"plan_id": binding["plan_id"], "application_id": base["application_id"],
               "image_digest": binding["image_digest"], "tests": list(GATE_RECHECK_TESTS)}
    steps.append({"step": "gate_recheck", "status": "ok", "detail": ",".join(GATE_RECHECK_TESTS)})
    return done("APPLIED", "APPLIED_AS_APPROVED", change, deployed["deployed_digest"], True, recheck)


def _write_receipt(directory: Path, result: ApplyOutcome) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result.application_id}.json"
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if result.outcome != "REFUSED" else os.O_APPEND)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
