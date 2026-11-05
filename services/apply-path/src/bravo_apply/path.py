"""The approved-change path, end to end, as one deterministic function (design 7.1 step 6).

Entry points for T10 (contract "반영 접수와 재시작", ADR-0021 §7):

* ``apply(application_id, …)`` — called once on the first durable claim. Every decision it
  makes is recorded under ``<receipts_dir>/<application_id>.json``; a second call with the same
  id returns that record unchanged and never touches the cluster again.
* ``get_result(application_id, receipts_dir)`` — what was recorded, or ``None`` if ``apply``
  never started for that id. Used after response loss or restart instead of calling ``apply``.

Before the first cluster call a STARTED record (outcome ``UNKNOWN``) is created atomically and
exclusively. It is replaced by the final outcome when the run finishes. If the process dies in
between, the record stays ``UNKNOWN``: nobody patches again, a person compares the Deployment.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path

import jsonschema

from .binding import IDENTIFIER, BindingError, verify_claim
from .deploy import DeployMismatch, Kube, KubectlKube, apply_pinned_image

SCHEMA_VERSION = "1.1"

# Controls the gate must ask again after the application changed how it reaches secrets and
# objects (docs/controls/README.md). The gate decides; T31 only requests the recheck.
GATE_RECHECK_TESTS = ["SEC-01.1", "IAM-01.1", "ENC-01.1"]

# A STARTED record older than this cannot still be running: rollout wait + 30 s, plus four
# other kubectl calls of at most KubectlKube.CALL_SECONDS each, plus slack.
_DEADLINE_SLACK = timedelta(seconds=30 + 4 * KubectlKube.CALL_SECONDS + 60)


class RecordUnreadable(RuntimeError):
    """A record exists for the id but does not parse or validate. The caller holds; a person
    looks. ``apply`` does not overwrite it."""


@dataclass
class ApplyOutcome:
    schema_version: str
    application_id: str
    approval_id: str
    verification_id: str
    plan_id: str
    outcome: str  # APPLIED | REFUSED | SAFE_HOLD | UNKNOWN (started, no final record)
    reason: str
    change: dict | None  # None until the binding is verified
    deployed_digest: str | None
    job_passed: bool
    gate_recheck: dict | None
    cluster: str  # "kubectl" | "fake" | "custom"
    target: dict
    cluster_mutation_attempted: bool | None  # REFUSED False, SAFE_HOLD/APPLIED True, UNKNOWN None
    previous_image: str | None
    observed_digests: list[str]
    started_at: str
    deadline_at: str | None
    finished_at: str | None
    steps: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, doc: dict) -> ApplyOutcome:
        return cls(**doc)

    def t10_application(self, *, allow_fake: bool = False) -> dict | None:
        """The exact fields T10 ``Application`` compares under the plan lock (workflow.py).

        Only for APPLIED on a real cluster. A ``FakeKube`` result is never reported as deployed
        unless a unit test asks for it explicitly."""
        if self.outcome != "APPLIED" or (self.cluster == "fake" and not allow_fake):
            return None
        return {"change": dict(self.change), "verification_id": self.verification_id, "approval_id": self.approval_id,
                "application_id": self.application_id, "job_passed": self.job_passed, "deployed_digest": self.deployed_digest}

    def failure_report(self) -> dict | None:
        """What T10 and a person need when the change is not applied: REFUSED, SAFE_HOLD or
        UNKNOWN. ``None`` for APPLIED. T10 holds the plan in REHEARSE on any of these."""
        if self.outcome == "APPLIED":
            return None
        return {"application_id": self.application_id, "approval_id": self.approval_id,
                "verification_id": self.verification_id, "outcome": self.outcome, "reason": self.reason,
                "change": None if self.change is None else dict(self.change), "cluster": self.cluster,
                "cluster_mutation_attempted": self.cluster_mutation_attempted,
                "previous_image": self.previous_image, "observed_digests": list(self.observed_digests),
                "deadline_at": self.deadline_at, "finished_at": self.finished_at}


@lru_cache(maxsize=1)
def _validator():
    schema = json.loads(resources.files("bravo_apply.schemas").joinpath("apply-result.schema.json").read_text("utf-8"))
    return jsonschema.Draft202012Validator(schema)


def validate_outcome(doc: dict) -> None:
    _validator().validate(doc)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _record_path(receipts_dir: Path, application_id: str) -> Path:
    if not isinstance(application_id, str) or not IDENTIFIER.fullmatch(application_id):
        raise ValueError("application_id must match " + IDENTIFIER.pattern)
    return Path(receipts_dir) / f"{application_id}.json"


def get_result(application_id: str, receipts_dir: Path) -> ApplyOutcome | None:
    """The recorded outcome for ``application_id``, or ``None`` if ``apply`` never started.

    Pure read. Never calls the cluster. Raises ``RecordUnreadable`` for a corrupt record."""
    path = _record_path(receipts_dir, application_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(raw)
        validate_outcome(doc)
    except (ValueError, jsonschema.ValidationError) as exc:
        raise RecordUnreadable(f"{path.name}: {type(exc).__name__}") from exc
    if doc["application_id"] != application_id:
        raise RecordUnreadable(f"{path.name}: application_id differs from file name")
    return ApplyOutcome.from_dict(doc)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory: Path, result: ApplyOutcome) -> str:
    doc = result.as_dict()
    validate_outcome(doc)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{result.application_id}.", suffix=".tmp", dir=directory)
    try:
        os.write(fd, (json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _create_record(directory: Path, result: ApplyOutcome) -> bool:
    """Atomically create the record only if none exists (complete bytes or nothing).

    ``link`` fails with EEXIST like ``O_EXCL`` but publishes a fully written, fsynced file, so a
    crash can never leave a half-written record. Returns False if another call got there first."""
    path = _record_path(directory, result.application_id)
    tmp = _write_temp(directory, result)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    _fsync_dir(directory)
    return True


def _replace_record(directory: Path, result: ApplyOutcome) -> None:
    """Replace this run's own STARTED record with its final outcome (atomic rename)."""
    os.replace(_write_temp(directory, result), _record_path(directory, result.application_id))
    _fsync_dir(directory)


def apply(application_id: str, claim: dict, proposal: dict, *, kube: Kube, receipts_dir: Path,
          target_environment_id: str, namespace: str, deployment: str, container: str, repository: str,
          sources: dict[str, str] | None = None, diff: bytes | None = None, now: datetime | None = None,
          timeout_seconds: int = 300, clock: Callable[[], datetime] | None = None) -> ApplyOutcome:
    """Apply the approved change at most once per ``application_id`` and record the outcome.

    If anything is already recorded for the id, that record is returned as-is (same fields)
    and the cluster is not called. ``now`` is the time the approval binding is checked at;
    ``clock`` stamps ``finished_at``."""
    receipts_dir = Path(receipts_dir)
    clock = clock or (lambda: datetime.now(UTC))
    existing = get_result(application_id, receipts_dir)  # also validates the id format
    if existing is not None:
        return existing
    now = now or clock()
    binding_in = claim.get("binding") if isinstance(claim.get("binding"), dict) else {}
    base = {"schema_version": SCHEMA_VERSION, "application_id": application_id,
            "approval_id": _ident(claim.get("approval_id")), "verification_id": _ident(claim.get("verification_id")),
            "plan_id": _ident(binding_in.get("plan_id")), "cluster": _cluster(kube),
            "target": {"namespace": namespace, "deployment": deployment, "container": container, "repository": repository},
            "started_at": _iso(now)}
    steps: list[dict] = []

    def outcome(kind, reason, *, change, mutation, deployed=None, recheck=None, previous=None, observed=(),
                deadline=None, finished=True):
        return ApplyOutcome(**base, outcome=kind, reason=reason, change=change, deployed_digest=deployed,
                            job_passed=kind == "APPLIED", gate_recheck=recheck, cluster_mutation_attempted=mutation,
                            previous_image=previous, observed_digests=list(observed), deadline_at=deadline,
                            finished_at=_iso(clock()) if finished else None, steps=list(steps))

    def first_record(result):
        """Publish a record only if none exists; otherwise the earlier one wins."""
        if _create_record(receipts_dir, result):
            return result
        return get_result(application_id, receipts_dir)

    # 1. binding: the id T10 passed ↔ claim ↔ proposal ↔ target ↔ time. Pure; no cluster call.
    try:
        if claim.get("application_id") != application_id:
            raise BindingError("APPLICATION_ID_MISMATCH")
        binding = verify_claim(claim, proposal, target_environment_id=target_environment_id, now=now, sources=sources, diff=diff)
    except BindingError as exc:
        steps.append({"step": "binding", "status": "refused", "detail": exc.code})
        return first_record(outcome("REFUSED", exc.code, change=None, mutation=False))
    change = {"diff_hash": binding["diff_hash"], "image_digest": binding["image_digest"]}
    steps.append({"step": "binding", "status": "ok", "detail": f"{len(binding['approved_paths'])} approved path(s)"})
    # 2. the image is the rehearsal-verified digest; nothing is built here
    steps.append({"step": "image", "status": "ok", "detail": "promote rehearsal-verified digest, no rebuild"})
    # 3. durable STARTED record before any cluster call; losing the race means someone else runs it
    deadline = _iso(now + timedelta(seconds=timeout_seconds) + _DEADLINE_SLACK)
    started = outcome("UNKNOWN", "APPLY_STARTED_NOT_FINISHED", change=change, mutation=None, deadline=deadline, finished=False)
    if not _create_record(receipts_dir, started):
        return get_result(application_id, receipts_dir)
    steps.append({"step": "started", "status": "ok", "detail": "durable record before any cluster call"})
    # 4. deploy pinned to the digest and confirm what is running
    try:
        deployed = apply_pinned_image(kube, namespace=namespace, deployment=deployment, container=container,
                                      repository=repository, digest=binding["image_digest"], timeout_seconds=timeout_seconds)
    except DeployMismatch as exc:
        detail = exc.code + (f" ({exc.detail})" if exc.detail else "")
        if not exc.mutation_attempted:
            steps.append({"step": "deploy", "status": "refused", "detail": detail})
            final = outcome("REFUSED", exc.code, change=change, mutation=False, observed=exc.observed)
        else:
            steps.append({"step": "deploy", "status": "failed", "detail": detail})
            # Nothing is rolled back automatically: the previous digest is restored only after a person looks (design 8.2).
            final = outcome("SAFE_HOLD", exc.code, change=change, mutation=True, previous=exc.previous_image,
                            observed=exc.observed)
        _replace_record(receipts_dir, final)
        return final
    steps.append({"step": "deploy", "status": "ok", "detail": f"{deployed['pods']} pod(s) on approved digest"})
    # 5. ask the gate to re-check the controls this change touches
    recheck = {"plan_id": binding["plan_id"], "application_id": application_id,
               "image_digest": binding["image_digest"], "tests": list(GATE_RECHECK_TESTS)}
    steps.append({"step": "gate_recheck", "status": "ok", "detail": ",".join(GATE_RECHECK_TESTS)})
    final = outcome("APPLIED", "APPLIED_AS_APPROVED", change=change, mutation=True, deployed=deployed["deployed_digest"],
                    recheck=recheck, previous=deployed["previous_image"], observed=deployed["observed"])
    _replace_record(receipts_dir, final)
    return final


def apply_approved_change(claim: dict, proposal: dict, *, kube: Kube, target_environment_id: str, namespace: str,
                          deployment: str, container: str, repository: str, sources: dict[str, str] | None = None,
                          diff: bytes | None = None, receipts_dir: Path | None = None,
                          now: datetime | None = None, timeout_seconds: int = 300) -> ApplyOutcome:
    """Earlier entry point (ADR-0021 §3): ``apply`` with the id taken from the claim.

    ``receipts_dir`` is now required: without a durable record there is no at-most-once."""
    if receipts_dir is None:
        raise TypeError("receipts_dir is required: the outcome must be recorded per application_id")
    return apply(str(claim.get("application_id", "")), claim, proposal, kube=kube, receipts_dir=receipts_dir,
                 target_environment_id=target_environment_id, namespace=namespace, deployment=deployment,
                 container=container, repository=repository, sources=sources, diff=diff, now=now,
                 timeout_seconds=timeout_seconds)


def _ident(value) -> str:
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else "unknown"


def _cluster(kube: Kube) -> str:
    value = getattr(kube, "cluster", "custom")
    return value if value in {"kubectl", "fake"} else "custom"
