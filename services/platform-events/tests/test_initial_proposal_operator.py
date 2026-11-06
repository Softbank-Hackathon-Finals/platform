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


def relay_for(f, job_principal=ACTOR, fence=(ACTOR,)):
    config = {"job_principal": job_principal, "fence_principals": list(fence)}
    return lambda request: {"status": "PASS", **handle(f.reopen(), json.loads(json.dumps(request)), config)}


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
    legacy_ref = tmp_path / "legacy-ref.json"
    legacy_ref.write_text(json.dumps(f.ref))
    out = operator.run(args(f, tmp_path, "fence", legacy_intent=str(legacy), legacy_principal=ACTOR,
                            legacy_ref=str(legacy_ref)), relay=relay_for(f), client=client)
    assert out["fenced"]
    with pytest.raises(ExecutionPending):
        operator.run(args(f, tmp_path, "start"), relay=relay_for(f), client=client)
    assert len(kube.creates) == 1


def _hold_or_pass(f, request):
    from bravo_platform_events.store import EventError

    try:
        return {"status": "PASS", **handle(f.reopen(), json.loads(json.dumps(request)),
                                           {"job_principal": ACTOR, "fence_principals": [ACTOR]})}
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


JOB = "b" * 64  # stands for the platform Job principal digest (b3d65883... live)


def test_actions_admission_fenced_then_job_principal_admitted_and_run(backend, tmp_path, monkeypatch):
    """The live shape: admission and claim under the Actions principal, response lost; the Job
    carrier needs its own principal, so the fence also frees the input for one re-admission."""
    import sqlite3

    from test_initial_proposal import EXPIRES, NOW

    f = Fixture(backend, tmp_path)
    kube = Kubectl()
    client = job.JobInitialProposal(tmp_path / "operator", image=IMAGE, source_revision=RUNTIME, kubectl=kube)
    f.client = LegacyActions(client)
    with pytest.raises(Exception):  # noqa: B017
        f.execution(runtime_source_sha=LEGACY_RUNTIME).execute(SPEC.plan_id, 3, **f.ref)
    monkeypatch.setattr(job, "legacy_actions_runs", lambda *a: dict(NEVER_RAN))
    legacy = tmp_path / "legacy.json"
    legacy.write_text(sqlite3.connect(client.path).execute("SELECT intent FROM executions").fetchone()[0])
    legacy_ref = tmp_path / "legacy-ref.json"
    legacy_ref.write_text(json.dumps(f.ref))
    new_ref = tmp_path / "job-ref.json"
    job_args = dict(principal=JOB, ref=str(new_ref))

    def job_run(command, **extra):
        a = args(f, tmp_path, command, **extra)
        a.principal, a.ref = JOB, str(new_ref)
        return operator.run(a, relay=relay_for(f, job_principal=JOB), client=client)

    with pytest.raises(Exception):  # noqa: B017 - no re-admission before the fence
        job_run("admit", tenant_id=SPEC.tenant_id, task_id="credential", expires_at=EXPIRES)
    job_run("fence", legacy_intent=str(legacy), legacy_principal=ACTOR, legacy_ref=str(legacy_ref))
    with pytest.raises(Exception):  # noqa: B017 - the Job principal has no T10 grant yet
        job_run("admit", tenant_id=SPEC.tenant_id, task_id="credential", expires_at=EXPIRES)
    f.access.set_grant(SPEC.plan_id, JOB, tenant_id=SPEC.tenant_id, enabled=True, valid_after=NOW,
                       expires_at=EXPIRES, expected_revision=0, administration_ref="fixture-management",
                       command_key="grant-job")
    first = job_run("admit", tenant_id=SPEC.tenant_id, task_id="credential", expires_at=EXPIRES)["ref"]
    assert job_run("admit", tenant_id=SPEC.tenant_id, task_id="credential", expires_at=EXPIRES)["ref"] == first
    with pytest.raises(ExecutionPending):
        job_run("start")
    assert len(kube.creates) == 1
    other = "c" * 64
    f.access.set_grant(SPEC.plan_id, other, tenant_id=SPEC.tenant_id, enabled=True, valid_after=NOW,
                       expires_at=EXPIRES, expected_revision=0, administration_ref="fixture-management",
                       command_key="grant-other")
    from bravo_platform_events.store import EventError

    with pytest.raises(EventError, match="initial_proposal_conflict"):  # a third principal needs another fence
        f.reopen().admit(other, plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id, task_id="credential",
                         expected_version=3, expires_at=EXPIRES)
    assert job_args


def test_relay_acts_only_as_its_configured_principals_and_rechecks_the_fence(backend, tmp_path, monkeypatch):
    f = Fixture(backend, tmp_path)
    config = {"job_principal": "b" * 64, "fence_principals": [ACTOR]}
    base = {"plan_id": SPEC.plan_id, "version": 3, "ref": f.ref, "runtime_source_sha": RUNTIME}
    with pytest.raises(ValueError, match="relay_principal_not_allowed"):  # model work only for the Job
        handle(f.reopen(), base | {"op": "fields", "principal": ACTOR}, config)
    with pytest.raises(ValueError, match="relay_principal_not_allowed"):  # fence only for named legacy
        handle(f.reopen(), base | {"op": "fence", "principal": "c" * 64, "proof": {}}, config)
    monkeypatch.setattr(job, "legacy_actions_runs", lambda *a: {"workflow_on_develop": True, "matching_runs": 0})
    from bravo_platform_events.initial_proposal_relay import _verified_fence

    with pytest.raises(ValueError, match="not_reproduced"):  # the platform does not trust the operator's proof
        _verified_fence({"proof": {"request_id": "t10p-x", "evidence": {"workflow_id": 4242}}})
