import hashlib
import json
from dataclasses import replace

import pytest

from rehearsal_runner.credential_proposal_source import BoundCredentialProposalSource
from rehearsal_runner.credential_verifier import CredentialVerificationRequest
from rehearsal_runner.runner import Namespace


class Source:
    def __init__(self, proposal):
        self.proposal = proposal
        self.digest = hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def result(self):
        return {"state": "PROPOSAL_READY", "attempt": 1, "run_id": "run-1", "source_sha": "a" * 40,
                "proposal_digest": self.digest, "proposal": self.proposal}


def make():
    proposal = {"run_id": "run-1", "source_sha": "a" * 40,
                "edits": [{"path": "app/db_secret.py", "before_sha256": "c" * 64,
                           "replacement": "private-source-marker"}]}
    source = Source(proposal)
    request = CredentialVerificationRequest("run-1", "a" * 40, source.digest, Namespace("rehearsal-x", "uid"))
    return source, request


def test_resolves_matching_proposal_without_exposing_source_in_repr():
    source, request = make()
    edits = BoundCredentialProposalSource(source).resolve(request)
    assert edits[0].replacement == "private-source-marker"
    assert "private-source-marker" not in repr(edits)


@pytest.mark.parametrize("field,value", [("run_id", "other"), ("source_sha", "d" * 40),
                                         ("proposal_digest", "e" * 64)])
def test_mismatched_request_identity_is_rejected(field, value):
    source, request = make()
    with pytest.raises(ValueError):
        BoundCredentialProposalSource(source).resolve(replace(request, **{field: value}))


def test_changed_proposal_with_original_digest_is_rejected():
    source, request = make()
    source.proposal["edits"][0]["replacement"] = "modified"
    with pytest.raises(ValueError):
        BoundCredentialProposalSource(source).resolve(request)


@pytest.mark.parametrize("paths", [["app/main.py"], ["app/db_secret.py", "outside.py"],
                                   ["app/db_secret.py", "app/db_secret.py"]])
def test_invalid_target_scope_is_rejected_even_with_matching_digest(paths):
    proposal = {"run_id": "run-1", "source_sha": "a" * 40,
                "edits": [{"path": p, "before_sha256": "c" * 64, "replacement": "safe"} for p in paths]}
    source = Source(proposal)
    request = CredentialVerificationRequest("run-1", "a" * 40, source.digest, Namespace("rehearsal-x", "uid"))
    with pytest.raises(ValueError):
        BoundCredentialProposalSource(source).resolve(request)


def test_runner_verifier_delivers_bound_edit_to_job():
    from rehearsal_runner.credential_verifier import CredentialVerifier
    from rehearsal_runner.isolation_gate import IsolationPass
    from rehearsal_runner.runner import ProposalRef, run_once
    from rehearsal_runner.secret_task import prepare_credential_migration

    source, request = make()
    task = prepare_credential_migration(request.run_id, request.source_sha)

    class Ports:
        def __init__(self):
            self.received = None

        def propose(self, task):
            return ProposalRef(task.run_id, task.source_sha, request.proposal_digest)

        def create(self, run_id):
            return request.namespace

        def check(self, namespace):
            return IsolationPass(namespace.name, namespace.uid, "probe-1")

        def verify(self, verification_request):
            self.received = BoundCredentialProposalSource(source).resolve(verification_request)
            return {"observation": "untrusted"}

        def cleanup(self, namespace):
            return True

    ports = Ports()
    result = run_once(task, ports, ports, ports, CredentialVerifier(task, ports))
    assert result.state == "RECEIPT_UNVERIFIED"
    assert ports.received[0].path == "app/db_secret.py"
    assert ports.received[0].replacement == "private-source-marker"
