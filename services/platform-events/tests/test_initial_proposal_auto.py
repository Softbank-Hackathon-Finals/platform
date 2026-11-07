"""PROPOSE without an operator: the host's own relay and Job identity, same single Job and stored result."""

import pytest
from test_cutover import SPEC
from test_initial_proposal import ACTOR, RUNTIME, Fixture
from test_initial_proposal_job import IMAGE, Kubectl, complete

from bravo_platform_events import initial_proposal_job as job
from bravo_platform_events.initial_proposal_auto import AutoInitialProposal, intent_path
from bravo_platform_events.store import EventError

CONFIG = {"job_principal": ACTOR, "fence_principals": [ACTOR]}


def _result(f, intent):
    from bravo_agent.contracts import Proposal, digest

    value = f.records.read_input(ACTOR, SPEC.plan_id, 3, input_ref=intent.input_ref, input_digest=intent.input_digest)
    proposal = Proposal(run_id=value["run_id"], source_sha=value["manifest"]["source_sha"], risk="low",
                        rationale="Synthetic initial proposal", edits=[dict(
                            path="app/config.py", before_sha256=value["manifest"]["files"]["app/config.py"],
                            replacement="VALUE = 2\n")])
    return dict(request_id=intent.request_id, input_digest=intent.input_digest, runtime_source_sha=RUNTIME,
                execution_receipt_id=f"github-{job.execution_id(intent.request_id)}-1",
                proposal=proposal.model_dump(), proposal_digest=digest(proposal), schema_valid=True, lint_passed=True)


def test_the_host_starts_one_job_holds_while_it_runs_then_stores_its_result(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    kube = Kubectl()
    auto = AutoInitialProposal(f.records, relay_config=CONFIG, state_dir=tmp_path / "host", image=IMAGE,
                               source_revision=RUNTIME, principal=ACTOR, kubectl=kube)
    with pytest.raises(EventError, match="initial_proposal_running"):
        auto(SPEC.plan_id, 3, f.ref)
    with pytest.raises(EventError, match="initial_proposal_running"):  # the next sweep reads, never resubmits
        auto(SPEC.plan_id, 3, f.ref)
    assert len(kube.creates) == 1

    intent = job.InitialIntent.model_validate_json(intent_path(tmp_path / "host", SPEC.plan_id, 3).read_text())
    result = _result(f, intent)
    complete(kube, intent, result, tmp_path)
    stored = auto(SPEC.plan_id, 3, f.ref)
    assert stored["proposal_digest"] == result["proposal_digest"] and stored["runtime_source_sha"] == RUNTIME
    assert f.port.stored(SPEC.plan_id, 3, **f.ref)["proposal_digest"] == result["proposal_digest"]
    assert auto(SPEC.plan_id, 3, f.ref)["proposal_digest"] == result["proposal_digest"]  # read again, same answer
    assert len(kube.creates) == 1


def test_the_relay_still_refuses_a_principal_the_host_config_does_not_name(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    auto = AutoInitialProposal(f.records, relay_config={"job_principal": "someone-else", "fence_principals": []},
                               state_dir=tmp_path / "host", image=IMAGE, source_revision=RUNTIME, principal=ACTOR,
                               kubectl=Kubectl())
    with pytest.raises(ValueError, match="relay_principal_not_allowed"):
        auto(SPEC.plan_id, 3, f.ref)


def test_admission_needs_exactly_one_registered_task(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    catalog = tmp_path / "auto-catalog"  # not the fixture's own catalog, which holds one task
    auto = AutoInitialProposal(f.records, relay_config=CONFIG, state_dir=tmp_path / "host", image=IMAGE,
                               source_revision=RUNTIME, principal=ACTOR, kubectl=Kubectl(), catalog_root=catalog)
    with pytest.raises(EventError, match="initial_task_unselected"):  # nothing registered for the plan
        auto.admit(SPEC.plan_id, SPEC.tenant_id, 3)
    (catalog / SPEC.plan_id).mkdir(parents=True)
    for task in ("task-a", "task-b"):
        (catalog / SPEC.plan_id / f"{task}.json").write_text("{}")
    with pytest.raises(EventError, match="initial_task_unselected"):  # two: S1 has not chosen
        auto.admit(SPEC.plan_id, SPEC.tenant_id, 3)



def test_one_registered_task_is_admitted_for_the_job_identity(backend, tmp_path):
    f = Fixture(backend, tmp_path, admit=False)
    auto = AutoInitialProposal(f.records, relay_config=CONFIG, state_dir=tmp_path / "host", image=IMAGE,
                               source_revision=RUNTIME, principal=ACTOR, kubectl=Kubectl(),
                               catalog_root=tmp_path / "catalog")  # the fixture registered exactly one task here
    ref = auto.admit(SPEC.plan_id, SPEC.tenant_id, 3)
    assert set(ref) == {"input_ref", "input_digest"}
    with pytest.raises(EventError, match="initial_proposal_running"):
        auto(SPEC.plan_id, 3, ref)  # and the Job starts from that admission


def test_the_in_cluster_calls_answer_like_kubectl():
    import json as _json

    from bravo_platform_events.initial_proposal_auto import InClusterKubectl

    calls, jobs = [], {}

    def send(method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            name = _json.loads(body)["metadata"]["name"]
            if name in jobs:
                return 409, b'{"reason":"AlreadyExists"}'
            jobs[name] = _json.loads(body)
            return 201, body
        if "/pods?" in path:
            return 200, _json.dumps({"items": [{"metadata": {"name": "j-1", "creationTimestamp": "1"}},
                                               {"metadata": {"name": "j-2", "creationTimestamp": "2"}}]}).encode()
        if path.endswith("/log?tailLines=1"):
            return 200, b'{"receipt": true}'
        if "/jobs?labelSelector=" in path:
            return 200, _json.dumps({"items": list(jobs.values())}).encode()
        name = path.rsplit("/", 1)[-1]
        return (200, _json.dumps(jobs[name]).encode()) if name in jobs else (404, b'{"reason":"NotFound"}')

    kube = InClusterKubectl(send=send)
    manifest = _json.dumps({"metadata": {"name": "bravo-initial-1"}}).encode()
    assert kube(["create", "-f", "-", "-o", "name"], manifest) == (0, b"job.batch/bravo-initial-1", b"")
    code, _, err = kube(["create", "-f", "-", "-o", "name"], manifest)
    assert code == 1 and b"AlreadyExists" in err  # JobInitialProposal reads the same execution, no second Job
    assert _json.loads(kube(["get", "jobs", "-l", "app.kubernetes.io/name=bravo-initial-proposal", "-o", "json"])[1])["items"]
    assert kube(["get", "job", "bravo-initial-1", "-o", "json"])[0] == 0
    assert kube(["get", "job", "missing", "-o", "json"])[0] == 1
    assert kube(["logs", "job/bravo-initial-1", "--tail", "1"]) == (0, b'{"receipt": true}', b"")
    assert calls[-1] == ("GET", "/api/v1/namespaces/bravo-initial/pods/j-2/log?tailLines=1")  # newest Pod
    assert all("/namespaces/bravo-initial/" in path for _, path in calls)  # never another namespace
    with pytest.raises(ValueError):
        kube(["delete", "job", "bravo-initial-1"])
