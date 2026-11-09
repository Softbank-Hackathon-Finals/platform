import copy
import hashlib
import json

import pytest

from rehearsal_runner.bucket_proposal_source import BoundBucketProposalSource
from rehearsal_runner.bucket_verifier import BucketVerificationRequest
from rehearsal_runner.runner import Namespace

SHA = "a" * 40
PATH = "src/target-policy.json"
PROPOSAL = {"run_id": "run-1", "source_sha": SHA,
            "edits": [{"path": PATH, "before_sha256": "e" * 64, "replacement": '{"Statement": []}'}]}


def proposal_digest(proposal):
    return hashlib.sha256(json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Source:
    def __init__(self):
        self.proposal = copy.deepcopy(PROPOSAL)
        self.output = {"state": "PROPOSAL_READY", "attempt": 1, "run_id": "run-1",
                       "source_sha": SHA, "proposal_digest": proposal_digest(self.proposal),
                       "proposal": self.proposal}

    def result(self):
        return self.output


def request(source):
    return BucketVerificationRequest("run-1", SHA, "c" * 64, source.output["proposal_digest"],
                                     "rehearsal-test", Namespace("rehearsal-test", "uid-1"))


def test_resolves_exact_proposal_and_preserves_binding():
    source = Source()
    value = BoundBucketProposalSource(source, PATH).resolve(request(source))
    assert value.replacement == PROPOSAL["edits"][0]["replacement"]
    assert value.target_bucket == "rehearsal-test" and value.source_policy_sha256 == "c" * 64
    assert value.proposal_digest == source.output["proposal_digest"]


@pytest.mark.parametrize("fault", ["state", "attempt", "run", "revision", "digest", "mutation", "path"])
def test_rejects_stale_or_changed_proposal(fault):
    source = Source()
    original = request(source)
    if fault == "state":
        source.output["state"] = "READY"
    elif fault == "attempt":
        source.output["attempt"] = 2
    elif fault == "run":
        source.output["run_id"] = "other"
    elif fault == "revision":
        source.output["source_sha"] = "b" * 40
    elif fault == "digest":
        source.output["proposal_digest"] = "0" * 64
    elif fault == "mutation":
        source.proposal["edits"][0]["replacement"] = "changed"
    else:
        source.proposal["edits"][0]["path"] = "src/other.json"
        source.output["proposal_digest"] = proposal_digest(source.proposal)
        original = request(source)
    with pytest.raises(ValueError):
        BoundBucketProposalSource(source, PATH).resolve(original)


@pytest.mark.parametrize("attempt", [2, 3])
def test_retry_resolves_only_matching_attempt(attempt):
    from dataclasses import replace

    source = Source()
    source.output["attempt"] = attempt
    original = request(source)
    value = BoundBucketProposalSource(source, PATH).resolve(replace(original, attempt=attempt))
    assert value.proposal_digest == original.proposal_digest
    with pytest.raises(ValueError):
        BoundBucketProposalSource(source, PATH).resolve(original)


def test_bucket_proposal_representation_hides_replacement():
    source = Source()
    value = BoundBucketProposalSource(source, PATH).resolve(request(source))
    assert PROPOSAL["edits"][0]["replacement"] not in repr(value)
