"""T10 #366 stored initial proposal → T15 planner. Run from services/agent-worker."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from bravo_agent.contracts import Proposal, SnapshotManifest, digest
from bravo_agent.subscription_product import ProductInput

from rehearsal_runner.attempt_store import collect_tool_events
from rehearsal_runner.bucket_proposer import T11BucketProposer
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.remote_proposals import bucket_product_bundle
from rehearsal_runner.stored_proposals import StoredProposalSource

SOURCE_PATH, TARGET_PATH = "src/source-policy.json", "src/target-policy.json"
SOURCE = json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                    "Resource": "arn"}]}, sort_keys=True)
RESTRICTED = json.dumps({"Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
                                        "Resource": "arn"}]}, sort_keys=True)
RUN = "proposal-" + "a" * 48  # T10 uses the stored proposal's run ID as the first rehearsal ID


def setup(tmp_path, *, change=None, path=TARGET_PATH):
    admission = SimpleNamespace(rehearsal_id=RUN, attempt=1)
    task = prepare_bucket_policy_task(RUN, SOURCE, "rehearsal-attachments", source_sha="1" * 40)
    files = {SOURCE_PATH: SOURCE, TARGET_PATH: SOURCE}
    manifest = SnapshotManifest(source_sha="1" * 40, editable_paths=[TARGET_PATH],
                                files={p: hashlib.sha256(c.encode()).hexdigest() for p, c in files.items()})
    value = ProductInput(admission_digest="a" * 64, attempt=1, bundle=bucket_product_bundle(task, SOURCE_PATH, TARGET_PATH),
                         manifest=manifest, files=files)
    proposal = Proposal.model_validate({
        "run_id": RUN, "source_sha": "1" * 40, "risk": "low", "rationale": "Translate the S3 policy",
        "edits": [{"path": path, "before_sha256": manifest.files[path], "replacement": RESTRICTED}]})
    stored = {"state": "PROPOSAL_READY", "attempt": 1, "run_id": RUN, "source_sha": "1" * 40,
              "proposal": proposal.model_dump(mode="json"), "proposal_digest": digest(proposal)} | (change or {})
    reads = []
    source = StoredProposalSource(product_input=lambda _a, _t: value,
                                  stored=lambda a: reads.append(a) or stored, staging=tmp_path)
    return admission, task, source, reads


def test_stored_proposal_reaches_the_planner_after_local_t11_recheck(tmp_path):
    admission, task, source, reads = setup(tmp_path)
    coordinator, provider = source.open(admission, task)
    proposer = T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH)
    ref = proposer.propose(task)
    assert coordinator.state == "PROPOSAL_READY" and reads == [admission]
    assert ref.proposal_digest == digest(coordinator.proposal)
    assert collect_tool_events(proposer, admission) == {"tool_events_status": "UNAVAILABLE"}


@pytest.mark.parametrize("change", [
    {"attempt": 2}, {"run_id": "proposal-other"}, {"source_sha": "2" * 40}, {"state": "NEEDS_HUMAN"},
    {"proposal_digest": "f" * 64},
])
def test_unbound_stored_result_stops_before_staging(tmp_path, change):
    admission, task, source, _ = setup(tmp_path, change=change)
    with pytest.raises(ValueError, match="not bound"):
        source.open(admission, task)
    assert not any(tmp_path.iterdir())


def test_local_t11_policy_still_applies_to_the_stored_proposal(tmp_path):
    # Schema-valid but outside the snapshot's editable paths: only T11's policy can refuse it.
    admission, task, source, _ = setup(tmp_path, path=SOURCE_PATH)
    coordinator, provider = source.open(admission, task)
    with pytest.raises(ValueError, match="T11 proposal unavailable"):
        T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH).propose(task)
    assert coordinator.proposal is None


def test_later_attempts_never_use_the_stored_proposal(tmp_path):
    admission, task, source, reads = setup(tmp_path)
    with pytest.raises(ValueError, match="first attempt"):
        source.open(SimpleNamespace(rehearsal_id=RUN, attempt=2), task)
    _, provider = source.open(admission, task)
    with pytest.raises(ValueError, match="first attempt"):
        asyncio.run(provider.propose(SimpleNamespace(), SimpleNamespace(attempt=2)))
    assert reads == [admission]
