"""The 10:08 request: claimed in T10, sent once through the removed Actions path, never received.

Fence it with proof, then the Job carrier runs exactly one new request for the same input.
"""

import json
import sqlite3

import pytest
from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown
from test_cutover import SPEC
from test_initial_proposal import RUNTIME, Fixture
from test_initial_proposal_job import IMAGE, Kubectl

from bravo_platform_events import initial_proposal_job as job
from bravo_platform_events.store import EventError

LEGACY_RUNTIME = "b" * 40  # the Actions-era develop head the original intent is bound to
NEVER_RAN = {"workflow_on_develop": False, "matching_runs": 0, "workflow_id": 4242}


class LegacyActions:
    """The removed carrier: it wrote its pre-POST ledger row, then the response was lost."""

    def __init__(self, client):
        self.client = client

    def recipient(self, request_id):
        return self.client.recipient(request_id)

    def submit(self, intent):
        with sqlite3.connect(self.client.path) as db:
            db.execute("INSERT INTO executions VALUES (?,?,?,NULL)", (intent.request_id,
                json.dumps(intent.model_dump()), json.dumps({"actor_id": 1, "workflow_id": 4242,
                                                             "dispatch_sent_at": "2026-11-06T01:08:09+00:00"})))
        raise ExecutionUnknown("DISPATCH_RESPONSE_UNCONFIRMED_READ_SAME_EXECUTION")

    def read(self, intent):
        raise ExecutionUnknown("RUN_NOT_YET_OBSERVED")

    def validate(self, intent, value, envelope):
        raise AssertionError("never reached")


@pytest.fixture
def legacy(backend, tmp_path, monkeypatch):
    f = Fixture(backend, tmp_path)
    kube = Kubectl()
    client = job.JobInitialProposal(tmp_path / "operator", image=IMAGE, source_revision=RUNTIME, kubectl=kube)
    f.client = LegacyActions(client)
    old = f.execution(runtime_source_sha=LEGACY_RUNTIME)
    with pytest.raises(ExecutionUnknown):
        old.execute(SPEC.plan_id, 3, **f.ref)
    f.client = client
    port = f.execution()
    evidence = {"value": dict(NEVER_RAN)}
    monkeypatch.setattr(job, "legacy_actions_runs", lambda workflow_id, request_id: dict(evidence["value"]))
    return f, port, client, kube, evidence


def test_original_request_is_stuck_until_fenced_then_one_new_job_runs(legacy):
    f, port, client, kube, _ = legacy
    with pytest.raises(EventError, match="initial_execution_conflict"):
        port.execute(SPEC.plan_id, 3, **f.ref)  # same slot, other runtime: nothing is sent
    assert kube.creates == []
    proof = port.fence_unreceived(SPEC.plan_id, 3, **f.ref)
    assert proof["kind"] == "initial-unreceived-fence-v1" and proof["dispatch_http_status"] is None
    with pytest.raises(ExecutionPending):
        port.execute(SPEC.plan_id, 3, **f.ref)
    assert len(kube.creates) == 1
    new_request = json.loads(kube.creates[0]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"])
    assert new_request["request_id"] != proof["request_id"] and new_request["runtime_source_sha"] == RUNTIME
    with pytest.raises(EventError, match="initial_execution_complete|initial_fence_unconfirmed"):
        port.fence_unreceived(SPEC.plan_id, 3, **f.ref)  # the new claim is a live Job, not fenceable
    with pytest.raises(ExecutionUnknown):  # the fenced request id can never run on the Job carrier
        client.submit(job.InitialIntent(**json.loads(
            sqlite3.connect(client.path).execute("SELECT intent FROM executions WHERE request_id=?",
                                                 (proof["request_id"],)).fetchone()[0])))
    assert len(kube.creates) == 1


@pytest.mark.parametrize("change", [
    {"workflow_on_develop": True}, {"matching_runs": 1}])
def test_fence_needs_the_legacy_workflow_gone_and_no_run(legacy, change):
    f, port, client, kube, evidence = legacy
    evidence["value"] |= change
    with pytest.raises(EventError, match="initial_fence_unconfirmed"):
        port.fence_unreceived(SPEC.plan_id, 3, **f.ref)
    with pytest.raises(EventError, match="initial_execution_conflict"):
        port.execute(SPEC.plan_id, 3, **f.ref)
    assert kube.creates == []


def test_an_accepted_dispatch_is_never_fenced(legacy):
    f, port, client, kube, _ = legacy
    with sqlite3.connect(client.path) as db:
        row = db.execute("SELECT request_id, metadata FROM executions").fetchone()
        metadata = json.loads(row[1]) | {"dispatch_response": {"http_status": 204}}
        db.execute("UPDATE executions SET metadata=? WHERE request_id=?", (json.dumps(metadata), row[0]))
    with pytest.raises(EventError, match="initial_fence_unconfirmed"):
        port.fence_unreceived(SPEC.plan_id, 3, **f.ref)
    assert kube.creates == []


def test_fence_is_idempotent_and_kept_in_both_ledgers(legacy, backend):
    f, port, client, kube, _ = legacy
    first = port.fence_unreceived(SPEC.plan_id, 3, **f.ref)
    assert client.fence_unreceived(job.InitialIntent(**json.loads(
        sqlite3.connect(client.path).execute("SELECT intent FROM executions").fetchone()[0]))) == first
    with backend.store._transaction() as c:
        keys = [r["command_key"] for r in c.execute(
            "SELECT command_key FROM plan_commands WHERE command_key LIKE ?", ("initial-proposal:3:execution%",))]
    assert sorted(keys) == ["initial-proposal:3:execution", "initial-proposal:3:execution:fence:1"]
