"""T38 #280 product response → T15 planner. Run from services/agent-worker."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_agent.contracts import Proposal, SnapshotManifest, digest
from bravo_agent.subscription_product import ProductInput, proposal_result, stage_input

from rehearsal_runner.attempt_store import collect_tool_events
from rehearsal_runner.bucket_proposer import T11BucketProposer
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.remote_proposals import RemoteProposalSource, bucket_product_bundle

SOURCE_PATH, TARGET_PATH = "src/source-policy.json", "src/target-policy.json"
SOURCE = json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                                    "Resource": "arn"}]}, sort_keys=True)
RESTRICTED = json.dumps({"Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
                                        "Resource": "arn"}]}, sort_keys=True)


class T38:
    """Builds responses with T38's own `proposal_result` (#280)."""

    def __init__(self, value, tmp_path, *, tamper=None):
        self.value, self.tmp_path, self.tamper, self.calls = value, tmp_path, tamper, []

    def __call__(self, body):
        self.calls.append(body)
        value = self.value
        target_sha = hashlib.sha256(value.files[TARGET_PATH].encode()).hexdigest()
        proposal = Proposal.model_validate({
            "run_id": value.bundle.run_id, "source_sha": value.bundle.source_sha, "risk": "low",
            "rationale": "Translate the S3 policy", "edits": [
                {"path": TARGET_PATH, "before_sha256": target_sha, "replacement": RESTRICTED}]})
        raw = {"state": "PROPOSAL_READY", "attempt": 1, "run_id": value.bundle.run_id,
               "source_sha": value.bundle.source_sha, "input": value.bundle.model_dump(),
               "input_digest": digest(value.bundle), "proposal": proposal.model_dump(),
               "proposal_digest": digest(proposal), "snapshot_digest": digest(value.manifest),
               "task": value.bundle.model_dump(), "task_digest": digest(value.bundle)}
        root = Path(self.tmp_path) / f"t38-{len(self.calls)}"
        root.mkdir()
        expected = {"product_input_digest": body["input_digest"], "admission_digest": value.admission_digest,
                    "runtime_source_sha": "f" * 40}
        response = proposal_result(raw, value, stage_input(value, root), expected)
        return 200, response | (self.tamper or {})


def setup(tmp_path, *, tamper=None, body_digest=None):
    admission = SimpleNamespace(rehearsal_id="run-1", attempt=1)
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-attachments", source_sha="1" * 40)
    files = {SOURCE_PATH: SOURCE, TARGET_PATH: SOURCE}
    manifest = SnapshotManifest(source_sha="1" * 40, editable_paths=[TARGET_PATH],
                                files={p: hashlib.sha256(c.encode()).hexdigest() for p, c in files.items()})
    value = ProductInput(admission_digest="a" * 64, attempt=1,
                         bundle=bucket_product_bundle(task, SOURCE_PATH, TARGET_PATH),
                         manifest=manifest, files=files)
    call = T38(value, tmp_path, tamper=tamper)
    source = RemoteProposalSource(
        product_input=lambda _a, _t: value, call=call, staging=tmp_path,
        request_for=lambda _a, v: {"input_ref": "input-1", "input_digest": body_digest or digest(v),
                                   "web_identity_token": "token"})
    return admission, task, source, call


def test_t38_proposal_reaches_the_planner_after_local_t11_recheck(tmp_path):
    admission, task, source, call = setup(tmp_path)
    coordinator, provider = source.open(admission, task)
    proposer = T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH)
    ref = proposer.propose(task)
    assert coordinator.state == "PROPOSAL_READY" and len(call.calls) == 1
    assert ref.proposal_digest == digest(coordinator.proposal)
    assert collect_tool_events(proposer, admission) == {"tool_events_status": "UNAVAILABLE"}


@pytest.mark.parametrize("tamper", [
    {"admission_digest": "b" * 64}, {"attempt": 2}, {"run_id": "run-2"},
    {"tool_events_status": "COMPLETE"}, {"state": "NEEDS_HUMAN"},
])
def test_unbound_t38_response_stops_before_any_effect(tmp_path, tamper):
    admission, task, source, _ = setup(tmp_path, tamper=tamper)
    coordinator, provider = source.open(admission, task)
    with pytest.raises(ValueError, match="T11 proposal unavailable"):
        T11BucketProposer(coordinator, provider, SOURCE_PATH, TARGET_PATH).propose(task)
    assert coordinator.state == "NEEDS_HUMAN" and coordinator.proposal is None


def test_later_attempt_never_calls_t38(tmp_path):
    admission, task, source, call = setup(tmp_path)
    _, provider = source.open(admission, task)
    with pytest.raises(ValueError, match="first attempt"):
        asyncio.run(provider.propose(SimpleNamespace(), SimpleNamespace(attempt=2)))
    assert call.calls == []


def test_request_must_carry_the_digest_of_the_same_input(tmp_path):
    admission, task, source, _ = setup(tmp_path, body_digest="c" * 64)
    with pytest.raises(ValueError, match="not bound"):
        source.open(admission, task)


@pytest.mark.parametrize("escape", ["../escaped.txt", "ABSOLUTE"])
def test_files_outside_the_manifest_are_refused_before_any_write(tmp_path, escape):
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "abs-escaped.txt"
    name = str(outside) if escape == "ABSOLUTE" else escape
    admission, task, source, call = setup(staging)
    value = source.product_input(admission, task)
    tampered = ProductInput.model_validate(value.model_dump() | {"files": value.files | {name: "x"}})
    source = RemoteProposalSource(product_input=lambda _a, _t: tampered, call=call, staging=staging,
                                  request_for=lambda _a, v: {"input_ref": "input-1", "input_digest": digest(v),
                                                             "web_identity_token": "token"})
    with pytest.raises(ValueError):
        source.open(admission, task)
    assert not outside.exists() and not (tmp_path / "escaped.txt").exists()
    assert call.calls == []
