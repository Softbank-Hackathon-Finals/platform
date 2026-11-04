"""T15 integrated control flow with synthetic T11, T38 and T10 ports."""

import hashlib
import json
from types import SimpleNamespace

from rehearsal_runner.approval_handoff import prepare_candidate
from rehearsal_runner.credential_candidate import prepare_candidate_files
from rehearsal_runner.credential_proposal_source import BoundCredentialProposalSource
from rehearsal_runner.credential_proposer import T11CredentialProposer, credential_summary
from rehearsal_runner.credential_verifier import CredentialVerificationRequest
from rehearsal_runner.integration_handoff import HandoffVerifier
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.retry import AttemptPorts, RetryDecision, run_retries
from rehearsal_runner.runner import Namespace
from rehearsal_runner.secret_task import prepare_credential_migration


class Snapshot:
    def read(self, path):
        return "fixture source"


class FakeCoordinator:
    def __init__(self, task):
        self.bundle = SimpleNamespace(run_id=task.run_id, source_sha=task.source_sha,
                                      summary=credential_summary(task))
        self.state, self.attempt = "READY", 0
        self.snapshot = Snapshot()
        self.current = None

    async def propose(self, provider):
        proposal = {"run_id": self.bundle.run_id, "source_sha": self.bundle.source_sha,
                    "edits": [{"path": "app/db_secret.py",
                               "before_sha256": hashlib.sha256(b"fixture source").hexdigest(),
                               "replacement": "fixture replacement"}]}
        digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.current = {"state": "PROPOSAL_READY", "attempt": 1, "run_id": self.bundle.run_id,
                        "source_sha": self.bundle.source_sha, "proposal_digest": digest, "proposal": proposal}
        return self.current

    def result(self):
        return self.current


class Ports:
    def __init__(self, task):
        self.task = task
        self.coordinator = FakeCoordinator(task)
        self.handoff = None
        self.cleaned = False

    def prepare(self, attempt, feedback_ref):
        proposer = T11CredentialProposer(self.coordinator, object())
        verifier = HandoffVerifier(self, attempt, b"fixture diff", "sha256:" + "c" * 64, "env-onprem")
        return AttemptPorts(proposer, self, self, verifier)

    def create(self, run_id):
        return Namespace("rehearsal-fixture", "uid-1")

    def check(self, namespace):
        return IsolationPass(namespace.name, namespace.uid, "probe-1")

    def verify(self, handoff):
        self.handoff = handoff
        request = CredentialVerificationRequest(handoff.proposal.run_id, handoff.proposal.source_sha,
                                                handoff.proposal.proposal_digest, handoff.namespace)
        edits = BoundCredentialProposalSource(self.coordinator).resolve(request)
        files = prepare_candidate_files(self.coordinator.snapshot, edits)
        assert files[0].content == "fixture replacement"
        return {"verdict": "PASS"}

    def cleanup(self, namespace):
        self.cleaned = True
        return True

    def assess(self, receipt, proposal, attempt):
        assert self.cleaned
        return RetryDecision(proposal.run_id, proposal.source_sha, proposal.proposal_digest, attempt, "PASS")


def test_full_t15_flow_prepares_candidate_without_claiming_real_approval():
    task = prepare_credential_migration("run-1", "a" * 40)
    ports = Ports(task)
    outcome = run_retries(task, ports, ports)
    assert outcome.state == "RECEIPT_UNVERIFIED"
    candidate = prepare_candidate(ports.handoff, plan_id="plan-1", verification_id="verification-1",
                                  evidence_ref="evidence-1", cleanup_uid="uid-1", cleanup_confirmed=ports.cleaned)
    assert candidate.run_id == task.run_id and candidate.namespace_uid == candidate.cleanup_uid
    assert candidate.diff_hash == ports.handoff.diff_hash
    assert not hasattr(candidate, "binding")
