"""Assemble bucket-policy attempts against a real cluster for the T10 host.

The host injects the plan binding, the T11 proposal source (coordinator and
provider; the model call itself is T38's) and the runner's cluster identity.
This module creates no credentials, opens no network path and calls no model.
"""

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .attempt_store import AttemptStore, PlanBinding, RecordedAttemptSender, RecordedEvidence
from .bucket_job import BucketFixture, SeaweedBucketJob
from .bucket_proposal_source import BoundBucketProposalSource
from .bucket_proposer import T11BucketProposer
from .bucket_verifier import BucketVerifier
from .isolation.kube import Kubectl
from .isolation.profile import targets
from .namespace_lifecycle import NamespaceLifecycle
from .rehearsal_host import T15RehearsalHost, TaskCatalog
from .retry import AttemptPorts
from .runner import Lifecycle, T3Gate

_K8S_NAME = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"


@dataclass(frozen=True)
class BucketTarget:
    """Dedicated test bucket (T9) and the Secret the runner identity reads for it."""

    fixture: BucketFixture
    credential_namespace: str
    credential_secret: str
    access_key_field: str = "access"
    secret_key_field: str = "secret"

    def __post_init__(self):
        if any(re.fullmatch(_K8S_NAME, v) is None for v in (self.credential_namespace, self.credential_secret)):
            raise ValueError("invalid credential reference")


class ProposalSource(Protocol):
    def open(self, admission: object, task: object) -> tuple[object, object]:
        """Return the T11 coordinator (validated snapshot) and provider for a new rehearsal."""
        ...


@dataclass
class BucketPlanner:
    """T15 `AttemptPlanner` for bucket tasks; one coordinator per rehearsal in this process.

    A later attempt resumes only the coordinator this process opened. After a
    restart it fails before any effect and the attempt goes to a human.
    """

    profile: dict[str, str]
    target: BucketTarget
    proposals: ProposalSource = field(repr=False)
    bindings: Callable[[object], PlanBinding] = field(repr=False)
    kubectl: Kubectl = field(repr=False)
    kube: object = field(repr=False)  # T38 verification.Kube: create/get/call/cleanup
    isolation_image: str
    source_policy_path: str = "src/source-policy.json"
    target_policy_path: str = "src/target-policy.json"
    isolation_cleanup_seconds: float = 30
    job_deadline_seconds: int = 180
    cleaner: object | None = field(default=None, repr=False)  # UID cleaner; defaults to `kube`
    wrap_lifecycle: Callable[[Lifecycle], Lifecycle] | None = field(default=None, repr=False)
    _open: dict = field(default_factory=dict, repr=False)

    def binding(self, admission: object) -> PlanBinding:
        return self.bindings(admission)

    def factory(self, admission: object, task: object) -> "_BucketFactory":
        return _BucketFactory(self, admission, task)

    def credential(self) -> tuple[str, str]:
        data = self.kube.get("secret", self.target.credential_secret, self.target.credential_namespace)["data"]
        return (base64.b64decode(data[self.target.access_key_field]).decode(),
                base64.b64decode(data[self.target.secret_key_field]).decode())


@dataclass(frozen=True)
class _BucketFactory:
    planner: BucketPlanner
    admission: object
    task: object

    def prepare(self, attempt: int, feedback: str | None) -> AttemptPorts:
        p, run_id = self.planner, self.task.run_id
        if attempt == 1:
            if run_id in p._open:
                raise ValueError("rehearsal already opened")
            p._open[run_id] = p.proposals.open(self.admission, self.task)
        elif run_id not in p._open:
            raise ValueError("coordinator for this rehearsal is not in this process")
        coordinator, provider = p._open[run_id]
        if attempt > 1:
            _record_failed_attempt(coordinator, run_id, self.task.source_sha, attempt - 1, feedback)
        proposer = T11BucketProposer(coordinator, provider, p.source_policy_path, p.target_policy_path,
                                     attempt=attempt, feedback_digest=feedback)
        lifecycle = NamespaceLifecycle(p.kubectl, p.cleaner or p.kube, p.profile, attempt)
        job = SeaweedBucketJob(p.kube, BoundBucketProposalSource(coordinator, p.target_policy_path),
                               coordinator.snapshot, p.target.fixture, p.credential,
                               source_policy_path=p.source_policy_path,
                               deadline_seconds=p.job_deadline_seconds)
        return AttemptPorts(proposer, p.wrap_lifecycle(lifecycle) if p.wrap_lifecycle else lifecycle,
                            T3Gate(p.kubectl, targets(p.profile), p.isolation_image,
                                   cleanup_timeout=p.isolation_cleanup_seconds),
                            BucketVerifier(self.task, job, attempt))


BUCKET_RETRY_SUMMARY = ("Independent verifier rejected the previous policy. "
                        "Keep authenticated reads working and deny anonymous reads.")


def _record_failed_attempt(coordinator, run_id, source_sha, attempt, feedback,
                           summary: str = BUCKET_RETRY_SUMMARY) -> None:
    """Give T11 the signed FAIL of the previous attempt so it allows one bounded retry."""
    from bravo_agent.contracts import VerificationReceipt

    if not isinstance(feedback, str) or re.fullmatch(r"[a-f0-9]{64}", feedback) is None:
        raise ValueError("retry feedback required")
    failure = coordinator.bundle.model_copy(update={
        "evidence_digest": feedback, "failure_code": "TEMPLATE_TEST_FAILED", "summary": summary})
    state = coordinator.verify(VerificationReceipt(
        run_id=run_id, source_sha=source_sha, proposal_digest=coordinator.result()["proposal_digest"],
        attempt=attempt, verdict="FAIL", effects_known=True, evidence_digest=feedback, failure=failure))
    if state != "RETRY_ALLOWED":
        raise ValueError("T11 did not allow a retry")


def build_bucket_host(catalog: TaskCatalog, planner: BucketPlanner, store: AttemptStore, *,
                      deliver=None, dispatch=None) -> tuple[T15RehearsalHost, RecordedEvidence]:
    """T10 `RehearsalHost` and the reader for `ChangeIntake.read_original`/`tool_events`."""
    options = {"deliver": deliver} | ({"dispatch": dispatch} if dispatch is not None else {})
    evidence = RecordedEvidence(store)
    return T15RehearsalHost(catalog, RecordedAttemptSender(store, planner, **options), evidence), evidence
