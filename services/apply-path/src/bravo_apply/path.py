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

The approved binding decides the branch (``change_kind``): an app image change patches the app
Deployment to the rehearsal-verified digest; a bucket-policy change (``policy.py``) replaces the
SeaweedFS ``s3.json`` Secret and rolls that workload, and never changes an image. Both branches
share the record, the STARTED rule, REFUSED/SAFE_HOLD/UNKNOWN and ``get_result``.
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

from .binding import APP_IMAGE, BUCKET_POLICY, IDENTIFIER, POLICY_PATH, BindingError, verify_claim
from .deploy import DeployMismatch, Kube, KubectlKube, apply_pinned_image
from .policy import (
    PROBE_WINDOW_SECONDS,
    PolicyRefused,
    PolicyTarget,
)
from .targets import (
    RECHECK_TESTS,
    BucketPolicyStore,
    CloudPolicyTarget,
    exposure_closer,
    policy_applier,
    recheck_tests,
)
from .targets import target_kind as kind_of

SCHEMA_VERSION = "1.2"

# Controls the gate must ask again after the application changed how it reaches secrets and
# objects (docs/controls/README.md). The gate decides; T31 only requests the recheck. The lists
# per target kind are `targets.RECHECK_TESTS`; these names are the onprem rows (T10 compares them).
GATE_RECHECK_TESTS = RECHECK_TESTS[APP_IMAGE]["onprem"]
POLICY_GATE_RECHECK_TESTS = RECHECK_TESTS[BUCKET_POLICY]["onprem"]

# A STARTED record older than this cannot still be running: rollout wait + 30 s, plus four
# other kubectl calls of at most KubectlKube.CALL_SECONDS each, plus slack.
_DEADLINE_SLACK = timedelta(seconds=30 + 4 * KubectlKube.CALL_SECONDS + 60)
# The bucket-policy branch also waits for two probes (anonymous, signed) after the rollout.
_POLICY_PROBE_SLACK = timedelta(seconds=2 * (PROBE_WINDOW_SECONDS + 15))


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
    change: dict | None  # None until the binding is verified; bucket policy adds change_kind · policy_digest
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
    policy: dict | None = None  # bucket-policy branch only: s3.json digests and probe answers; absent in 1.1

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
        out = {"change": dict(self.change), "verification_id": self.verification_id, "approval_id": self.approval_id,
               "application_id": self.application_id, "job_passed": self.job_passed, "deployed_digest": self.deployed_digest}
        if self.change.get("change_kind") == BUCKET_POLICY:
            # No image was deployed, so there is no deployed digest to report. T10 must compare
            # applied_policy_digest with change.policy_digest instead (README "T10에 돌려주는 것").
            out["applied_policy_digest"] = self.change["policy_digest"]
        return out

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
                "policy": None if self.policy is None else dict(self.policy),
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
          target_environment_id: str, namespace: str = "", deployment: str = "", container: str = "app",
          repository: str = "", policy_target: PolicyTarget | CloudPolicyTarget | None = None,
          sources: dict[str, str] | None = None, diff: bytes | None = None, now: datetime | None = None,
          timeout_seconds: int = 300, clock: Callable[[], datetime] | None = None,
          policy_hooks: dict | None = None, policy_stores: dict[str, BucketPolicyStore] | None = None,
          image_target_kind: str = "onprem", pull_secret: str | None = None,
          exposure: dict | None = None) -> ApplyOutcome:
    """Apply the approved change at most once per ``application_id`` and record the outcome.

    If anything is already recorded for the id, that record is returned as-is (same fields)
    and the cluster is not called. ``now`` is the time the approval binding is checked at;
    ``clock`` stamps ``finished_at``. The binding's ``change_kind`` selects the branch:
    ``namespace``/``deployment``/``container``/``repository`` are the app image target,
    ``policy_target`` the bucket-policy target. ``policy_hooks`` is a test seam passed to the
    policy applier (probes, sleep, clock). The policy target's kind selects the applier
    (``targets.policy_applier``): onprem is the T9 ``s3.json`` path, aws/gcp need the matching
    ``policy_stores`` entry. ``image_target_kind`` names the target kind of an image change for
    the gate recheck."""
    receipts_dir = Path(receipts_dir)
    clock = clock or (lambda: datetime.now(UTC))
    existing = get_result(application_id, receipts_dir)  # also validates the id format
    if existing is not None:
        return existing
    now = now or clock()
    binding_in = claim.get("binding") if isinstance(claim.get("binding"), dict) else {}
    policy_kind = binding_in.get("change_kind") == BUCKET_POLICY and policy_target is not None
    base = {"schema_version": SCHEMA_VERSION, "application_id": application_id,
            "approval_id": _ident(claim.get("approval_id")), "verification_id": _ident(claim.get("verification_id")),
            "plan_id": _ident(binding_in.get("plan_id")), "cluster": _cluster(kube),
            "target": policy_target.record() if policy_kind else
            {"namespace": namespace, "deployment": deployment, "container": container, "repository": repository},
            "started_at": _iso(now)}
    steps: list[dict] = []

    def outcome(kind, reason, *, change, mutation, deployed=None, recheck=None, previous=None, observed=(),
                deadline=None, finished=True, policy=None):
        return ApplyOutcome(**base, outcome=kind, reason=reason, change=change, deployed_digest=deployed,
                            job_passed=kind == "APPLIED", gate_recheck=recheck, cluster_mutation_attempted=mutation,
                            previous_image=previous, observed_digests=list(observed), deadline_at=deadline,
                            finished_at=_iso(clock()) if finished else None, steps=list(steps), policy=policy)

    def first_record(result):
        """Publish a record only if none exists; otherwise the earlier one wins."""
        if _create_record(receipts_dir, result):
            return result
        return get_result(application_id, receipts_dir)

    # 1. binding: the id T10 passed ↔ claim ↔ proposal ↔ target ↔ time. Pure; no cluster call.
    try:
        if claim.get("application_id") != application_id:
            raise BindingError("APPLICATION_ID_MISMATCH")
        binding = verify_claim(claim, proposal, target_environment_id=target_environment_id, now=now, sources=sources,
                               diff=diff, policy_path=POLICY_PATH)
        if binding["change_kind"] == BUCKET_POLICY and policy_target is None:
            raise BindingError("POLICY_TARGET_MISSING")
        if binding["change_kind"] == APP_IMAGE and not (namespace and deployment and container and repository):
            raise BindingError("IMAGE_TARGET_MISSING")
    except BindingError as exc:
        steps.append({"step": "binding", "status": "refused", "detail": exc.code})
        return first_record(outcome("REFUSED", exc.code, change=None, mutation=False))
    is_policy = binding["change_kind"] == BUCKET_POLICY
    change = {"diff_hash": binding["diff_hash"], "image_digest": binding["image_digest"]}
    if is_policy:
        change |= {"change_kind": BUCKET_POLICY, "policy_digest": binding["policy_digest"]}
    steps.append({"step": "binding", "status": "ok", "detail": f"{len(binding['approved_paths'])} approved path(s)"})
    kind = kind_of(policy_target) if is_policy else image_target_kind
    if is_policy:
        # 1b. the approved policy checked by the target kind's applier (onprem: a T9 exposure state;
        # aws/gcp: a canonical neutral render). Pure; refused before a STARTED record exists.
        try:
            applier = policy_applier(policy_target, policy_stores)
            prepared = applier.prepare(proposal["edits"][0]["replacement"], policy_target)
        except PolicyRefused as exc:
            steps.append({"step": "policy_translate", "status": "refused", "detail": exc.code})
            return first_record(outcome("REFUSED", exc.code, change=change, mutation=False))
        steps.append({"step": "policy_translate", "status": "ok", "detail": prepared.detail})
        # 2. a bucket-policy change neither builds nor changes any image
        steps.append({"step": "image", "status": "skipped", "detail": "bucket-policy change: app image unchanged"})
    else:
        # 2. the image is the rehearsal-verified digest; nothing is built here
        steps.append({"step": "image", "status": "ok", "detail": "promote rehearsal-verified digest, no rebuild"})
    # 1c. ADR-0044: an approval that binds a closing T44 plan closes the bucket after the image, in this same
    # application. The plan must be the one the binding names, for the environment this applier is set for.
    # Pure; refused before a STARTED record exists.
    planned = binding_in.get("iac_plan_digest")
    if planned is not None or exposure is not None:
        from .desired import DesiredRefused

        try:
            if is_policy or planned is None or exposure is None:
                raise DesiredRefused("EXPOSURE_SCOPE_MISMATCH")
            # The close belongs to the target kind's applier: onprem only (T44's SeaweedFS s3.json). aws/gcp
            # are refused here, before a STARTED record, so an unsupported kind never changes the image either.
            closer = exposure_closer(kind, exposure["target"])
            # Git only: the plan is T44's exact close of the state it was planned against, and the approved one.
            closer.check_exposure(exposure, planned)
        except DesiredRefused as exc:
            steps.append({"step": "exposure_plan", "status": "refused", "detail": exc.code})
            return first_record(outcome("REFUSED", exc.code, change=change, mutation=False))
        steps.append({"step": "exposure_plan", "status": "ok", "detail": "approved T44 plan closes the bucket"})
    # 3. durable STARTED record before any cluster call; losing the race means someone else runs it
    slack = _DEADLINE_SLACK + (_POLICY_PROBE_SLACK if is_policy else timedelta())
    deadline = _iso(now + timedelta(seconds=timeout_seconds) + slack)
    started = outcome("UNKNOWN", "APPLY_STARTED_NOT_FINISHED", change=change, mutation=None, deadline=deadline, finished=False)
    if not _create_record(receipts_dir, started):
        return get_result(application_id, receipts_dir)
    steps.append({"step": "started", "status": "ok", "detail": "durable record before any cluster call"})
    # 4. deploy pinned to the digest (or apply the policy) and confirm by observation
    step = "policy_apply" if is_policy else "deploy"
    try:
        if is_policy:
            deployed = applier.apply(kube, policy_target, prepared, application_id=application_id,
                                     timeout_seconds=timeout_seconds, **(policy_hooks or {}))
        else:
            deployed = apply_pinned_image(kube, namespace=namespace, deployment=deployment, container=container,
                                          repository=repository, digest=binding["image_digest"], timeout_seconds=timeout_seconds,
                                          pull_secret=pull_secret)
    except DeployMismatch as exc:
        detail = exc.code + (f" ({exc.detail})" if exc.detail else "")
        if not exc.mutation_attempted:
            steps.append({"step": step, "status": "refused", "detail": detail})
            final = outcome("REFUSED", exc.code, change=change, mutation=False, observed=exc.observed, policy=exc.policy)
        else:
            steps.append({"step": step, "status": "failed", "detail": detail})
            # Nothing is rolled back automatically: the previous digest or s3.json is restored only after a
            # person looks (design 8.2).
            final = outcome("SAFE_HOLD", exc.code, change=change, mutation=True, previous=exc.previous_image,
                            observed=exc.observed, policy=exc.policy)
        _replace_record(receipts_dir, final)
        return final
    if is_policy:
        steps.append({"step": step, "status": "ok", "detail": "anonymous GET denied, app identity GET allowed"})
    else:
        steps.append({"step": step, "status": "ok", "detail": f"{deployed['pods']} pod(s) on approved digest"})
        if exposure is not None:
            # 4b. close after the image, so this application's finished_at (the post-apply run's lower bound)
            # is after both. A failure holds the whole application; the image is not rolled back (8.2).
            from .desired import DesiredRefused

            try:
                closed = closer.close_exposure(exposure, planned, kube=kube, receipts_dir=receipts_dir,
                                               plan_id=binding["plan_id"], timeout_seconds=timeout_seconds,
                                               policy_hooks=policy_hooks)
            except DesiredRefused as exc:
                # The image is already out: a refusal here (e.g. the Git read failing the second time) holds this
                # application instead of leaving its record STARTED.
                closed = {"application_id": "-", "status": "REFUSED", "reason": exc.code}
            if closed["status"] not in ("APPLIED", "ALREADY_CLOSED"):
                steps.append({"step": "exposure_close", "status": "failed",
                              "detail": f"{closed['application_id']} {closed['status']} {closed['reason']}"})
                # Same shape as any SAFE_HOLD: no deployed digest is reported; the observed digests show the image.
                final = outcome("SAFE_HOLD", "EXPOSURE_" + closed["status"], change=change, mutation=True,
                                previous=deployed["previous_image"], observed=deployed.get("observed", ()))
                _replace_record(receipts_dir, final)
                return final
            steps.append({"step": "exposure_close", "status": "ok",
                          "detail": f"{closed['application_id']} {closed['status']}"})
    tests = recheck_tests(binding["change_kind"], kind)
    # 5. ask the gate to re-check the controls this change touches
    recheck = {"plan_id": binding["plan_id"], "application_id": application_id,
               "image_digest": binding["image_digest"], "tests": list(tests)}
    if is_policy:
        recheck["policy_digest"] = binding["policy_digest"]
    if kind != "onprem":
        recheck["target_kind"] = kind  # the gate picks the (kind, test id) probe; onprem keeps T10's exact shape
    steps.append({"step": "gate_recheck", "status": "ok", "detail": ",".join(tests)})
    final = outcome("APPLIED", "APPLIED_AS_APPROVED", change=change, mutation=True,
                    deployed=None if is_policy else deployed["deployed_digest"], recheck=recheck,
                    previous=deployed["previous_image"], observed=deployed.get("observed", ()),
                    policy=deployed.get("policy"))
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
