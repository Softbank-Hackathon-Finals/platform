"""Assemble credential-migration attempts against a real cluster for the T10 host.

Same shape as `BucketPlanner`: the host injects the plan binding, the T11 proposal source and
the runner's cluster identity, plus T9's OpenBao delivery and T7's published candidate image.
`RoutedPlanner` lets one T10 host serve both task kinds.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from .attempt_store import PlanBinding
from .bucket_planner import ProposalSource, _record_failed_attempt
from .credential_job import CredentialFixture, CredentialJob, OpenBaoDelivery
from .credential_proposal_source import BoundCredentialProposalSource
from .credential_proposer import T11CredentialProposer
from .credential_verifier import CredentialVerifier
from .isolation.kube import Kubectl
from .isolation.profile import targets
from .namespace_lifecycle import NamespaceLifecycle
from .retry import AttemptPorts
from .runner import Lifecycle, T3Gate
from .secret_task import CredentialMigrationTask

CREDENTIAL_RETRY_SUMMARY = ("Independent verifier rejected the previous change: the app did not reach the "
                            "database with the injected /run/bravo/database/credentials.json. Read only that "
                            "file, fail closed when it is missing, and never log credentials.")


@dataclass
class CredentialPlanner:
    """T15 `AttemptPlanner` for credential tasks; one coordinator per rehearsal in this process."""

    profile: dict[str, str]
    proposals: ProposalSource = field(repr=False)
    bindings: Callable[[object], PlanBinding] = field(repr=False)
    kubectl: Kubectl = field(repr=False)
    kube: object = field(repr=False)  # T38 verification.Kube
    isolation_image: str
    fixture: CredentialFixture
    delivery: OpenBaoDelivery = field(repr=False)
    candidate_image: Callable[[str], str] = field(repr=False)  # diff hash -> T7-published image
    isolation_cleanup_seconds: float = 30
    job_deadline_seconds: int = 180
    cleaner: object | None = field(default=None, repr=False)
    wrap_lifecycle: Callable[[Lifecycle], Lifecycle] | None = field(default=None, repr=False)
    _open: dict = field(default_factory=dict, repr=False)

    def binding(self, admission: object) -> PlanBinding:
        return self.bindings(admission)

    def factory(self, admission: object, task: object) -> "_CredentialFactory":
        if not isinstance(task, CredentialMigrationTask):
            raise ValueError("credential task required")
        return _CredentialFactory(self, admission, task)


@dataclass(frozen=True)
class _CredentialFactory:
    planner: CredentialPlanner
    admission: object
    task: CredentialMigrationTask

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
            _record_failed_attempt(coordinator, run_id, self.task.source_sha, attempt - 1, feedback,
                                   CREDENTIAL_RETRY_SUMMARY)
        proposer = T11CredentialProposer(coordinator, provider, attempt=attempt, feedback_digest=feedback)
        lifecycle = NamespaceLifecycle(p.kubectl, p.cleaner or p.kube, p.profile, attempt)
        job = CredentialJob(p.kube, p.kubectl, BoundCredentialProposalSource(coordinator), coordinator.snapshot,
                            p.delivery, p.fixture, p.candidate_image, deadline_seconds=p.job_deadline_seconds)
        return AttemptPorts(proposer, p.wrap_lifecycle(lifecycle) if p.wrap_lifecycle else lifecycle,
                            T3Gate(p.kubectl, targets(p.profile), p.isolation_image,
                                   cleanup_timeout=p.isolation_cleanup_seconds),
                            CredentialVerifier(self.task, job, attempt))


@dataclass(frozen=True)
class RoutedPlanner:
    """One planner per task kind behind a single T10 host; the plan binding is shared."""

    bucket: object
    credential: object

    def binding(self, admission: object) -> PlanBinding:
        return self.bucket.binding(admission)

    def factory(self, admission: object, task: object):
        planner = self.credential if isinstance(task, CredentialMigrationTask) else self.bucket
        return planner.factory(admission, task)
