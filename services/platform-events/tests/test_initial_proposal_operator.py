"""Operator script + T10 relay end to end: claim, one Job, receipt stored by T10; fence of the 10:08 request."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_agent.product_execution_contracts import ExecutionPending
from test_cutover import SPEC
from test_initial_legacy_fence import LEGACY_RUNTIME, NEVER_RAN, LegacyActions
from test_initial_proposal import ACTOR, RUNTIME, Fixture
from test_initial_proposal_job import IMAGE, Kubectl, complete

from bravo_platform_events import initial_proposal_job as job
from bravo_platform_events.initial_proposal_relay import handle

SPEC_PATH = Path(__file__).resolve().parents[3] / "operations/initial-proposal/run.py"
spec = importlib.util.spec_from_file_location("initial_operator", SPEC_PATH)
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


def args(f, tmp_path, command, **extra):
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(f.ref))
    return SimpleNamespace(command=command, plan_id=SPEC.plan_id, version=3, ref=str(ref), principal=ACTOR,
                           state_dir=str(tmp_path / "operator"), **extra)


def relay_for(f):
    return lambda request: {"status": "PASS", **handle(f.reopen(), json.loads(json.dumps(request)))}


def test_start_then_collect_stores_the_job_result_in_t10(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    kube = Kubectl()
    client = job.JobInitialProposal(tmp_path / "operator", image=IMAGE, source_revision=RUNTIME, kubectl=kube)
    with pytest.raises(ExecutionPending):
        operator.run(args(f, tmp_path, "start"), relay=relay_for(f), client=client)
    assert len(kube.creates) == 1
    intent = job.InitialIntent.model_validate_json((tmp_path / "operator" / "intents" / f"{SPEC.plan_id}-3.json").read_text())
    value = f.records.read_input(ACTOR, SPEC.plan_id, 3, input_ref=intent.input_ref, input_digest=intent.input_digest)
    from bravo_agent.contracts import Proposal, digest

    proposal = Proposal(run_id=value["run_id"], source_sha=value["manifest"]["source_sha"], risk="low",
        rationale="Synthetic initial proposal", edits=[dict(path="app/config.py",
            before_sha256=value["manifest"]["files"]["app/config.py"], replacement="VALUE = 2\n")])
    run_id = job.execution_id(intent.request_id)
    result = dict(request_id=intent.request_id, input_digest=intent.input_digest, runtime_source_sha=RUNTIME,
                  execution_receipt_id=f"github-{run_id}-1", proposal=proposal.model_dump(),
                  proposal_digest=digest(proposal), schema_valid=True, lint_passed=True)
    complete(kube, intent, result, tmp_path)
    out = operator.run(args(f, tmp_path, "collect"), relay=relay_for(f), client=client)
    assert out["runtime_source_sha"] == RUNTIME and out["proposal_digest"] == result["proposal_digest"]
    assert f.port.stored(SPEC.plan_id, 3, **f.ref)["proposal_digest"] == result["proposal_digest"]
    assert len(kube.creates) == 1


def test_fence_then_start_runs_one_new_request(backend, tmp_path, monkeypatch):
    f = Fixture(backend, tmp_path)
    kube = Kubectl()
    client = job.JobInitialProposal(tmp_path / "operator", image=IMAGE, source_revision=RUNTIME, kubectl=kube)
    f.client = LegacyActions(client)
    with pytest.raises(Exception):  # noqa: B017 - the legacy carrier lost its response
        f.execution(runtime_source_sha=LEGACY_RUNTIME).execute(SPEC.plan_id, 3, **f.ref)
    monkeypatch.setattr(job, "legacy_actions_runs", lambda *a: dict(NEVER_RAN))
    import sqlite3

    legacy = tmp_path / "legacy.json"
    legacy.write_text(sqlite3.connect(client.path).execute("SELECT intent FROM executions").fetchone()[0])
    with pytest.raises(RuntimeError, match="hold"):  # the legacy claim blocks a new start
        operator.run(args(f, tmp_path, "start"), relay=lambda r: _hold_or_pass(f, r), client=client)
    out = operator.run(args(f, tmp_path, "fence", legacy_intent=str(legacy)), relay=relay_for(f), client=client)
    assert out["fenced"]
    with pytest.raises(ExecutionPending):
        operator.run(args(f, tmp_path, "start"), relay=relay_for(f), client=client)
    assert len(kube.creates) == 1


def _hold_or_pass(f, request):
    from bravo_platform_events.store import EventError

    try:
        return {"status": "PASS", **handle(f.reopen(), json.loads(json.dumps(request)))}
    except EventError as error:
        raise RuntimeError(f"t10_hold:{error.code}") from None


def test_from_file_expands_to_the_same_options(tmp_path):
    options = tmp_path / "initial.json"
    options.write_text(json.dumps({"plan_id": "p", "version": 3, "ref": "/r.json", "principal": "x",
                                   "state_dir": "/s", "context": "platform", "relay": ["a", "b"]}))
    argv = operator.expand(["start", "--from", str(options)])
    assert argv[:3] == ["start", "--plan-id", "p"] and argv[-3:] == ["--relay", "a", "b"]


def test_plan_state_reads_without_writing(backend):
    from test_workflow_recovery import collect

    from bravo_platform_events.plan_state import read

    collect(backend.store)
    assert read(backend.store, SPEC.plan_id) == {"plan_id": SPEC.plan_id, "state": "ANALYZE", "version": 2}
