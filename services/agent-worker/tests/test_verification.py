import asyncio
import copy
import json

import pytest

from bravo_agent import verification
from bravo_agent.contracts import Proposal
from bravo_agent.coordinator import Coordinator


def result_for(context):
    bundle, snapshot, proposal, _ = context

    class Provider:
        async def propose(self, bundle, tools):
            tools.dispatch("mcp__bravo__plan_propose", proposal)

    return asyncio.run(Coordinator(bundle, snapshot).propose(Provider()))


@pytest.mark.parametrize("deleted,complete,failure,expected", [
    (True, True, "NONE", "AWAITING_APPROVAL"),
    (False, True, "NONE", "NEEDS_HUMAN"),
    (True, False, "TEMPLATE_TEST_FAILED", "RETRY_ALLOWED"),
    (True, False, "VERIFIER_UNAVAILABLE", "NEEDS_HUMAN"),
])
def test_receipt_requires_job_success_and_confirmed_cleanup(context, monkeypatch, deleted, complete, failure, expected):
    bundle, snapshot, _, _ = context
    monkeypatch.setattr(verification, "verify_environment", lambda *args: {
        "namespace_deleted": deleted, "job_complete": complete, "failure_code": failure,
        "checks": {"injected_value": complete, "missing_value_closed": complete},
    })
    output = verification.integrate(bundle.model_dump(), snapshot.manifest.model_dump(), snapshot.root,
                                    result_for(context), None, "test-image")
    assert output["state"] == expected
    assert output["receipt"]["proposal_digest"] == result_for(context)["proposal_digest"]


@pytest.mark.parametrize("field,value", [("proposal_digest", "a" * 64), ("snapshot_digest", "b" * 64),
                                         ("attempt", 2), ("input_digest", "c" * 64)])
def test_unbound_result_cannot_create_a_job(context, monkeypatch, field, value):
    bundle, snapshot, _, _ = context
    result = copy.deepcopy(result_for(context))
    result[field] = value
    monkeypatch.setattr(verification, "verify_environment", lambda *args: pytest.fail("unbound proposal executed"))
    with pytest.raises(ValueError, match="unbound"):
        verification.integrate(bundle.model_dump(), snapshot.manifest.model_dump(), snapshot.root, result, None, "test-image")


def test_candidate_cannot_mount_hidden_verifier_or_use_cluster_identity():
    candidate = verification.job("test-ns", "present", "test-image", {"candidate": "/candidate", "wrapper": "/runner"}, {})
    verifier = verification.job("test-ns", "verify", "test-image", {"verifier": "/runner"}, {}, verifier=True)
    for template in (candidate, verifier):
        spec = template["spec"]["template"]["spec"]
        assert spec["automountServiceAccountToken"] is False
        assert spec["securityContext"]["runAsNonRoot"] is True
    assert "verifier" not in str(candidate)
    assert "candidate" not in str(verifier["spec"]["template"]["spec"]["volumes"])


def test_cleanup_uses_uid_precondition_and_requires_absence(monkeypatch):
    calls = []
    kube = verification.Kube()

    def call(*args, data=None):
        calls.append((args, data))
        return ""

    monkeypatch.setattr(kube, "call", call)
    assert kube.cleanup("rehearsal-t15-test", "original-uid")
    assert calls[0][1]["preconditions"] == {"uid": "original-uid"}
    assert "--ignore-not-found" in calls[1][0]


@pytest.mark.parametrize("field,value", [("run_id", "old-execution"), ("proposal_digest", "d" * 64)])
def test_requested_immutable_proposal_is_checked_before_any_job(context, monkeypatch, field, value):
    bundle, snapshot, _, _ = context
    monkeypatch.setattr(verification, "verify_environment", lambda *args: pytest.fail("wrong execution started"))
    with pytest.raises(ValueError, match="unbound"):
        verification.integrate(bundle.model_dump(), snapshot.manifest.model_dump(), snapshot.root,
                               result_for(context), None, "test-image", **{field: value})


IMAGE = "registry.invalid/python@sha256:" + "a" * 64


class ExecutionKube:
    """API responses with real Job/Pod relationships, including read/log race injections."""

    def __init__(self, fault=None, target="present", passed=True):
        self.fault, self.target, self.passed = fault, target, passed
        self.jobs, self.pods, self.logs, self.deleted = {}, {}, [], []

    def create(self, obj):
        obj = copy.deepcopy(obj)
        name = obj["metadata"]["name"]
        obj["metadata"]["uid"] = name + "-uid"
        if obj["kind"] == "Job":
            terminal = name == "verify"
            obj["status"] = {"conditions": [{"type": "Complete" if self.passed else "Failed", "status": "True"}]} if terminal else {}
            self.jobs[name] = obj
            container_id = "containerd://" + name
            state = {"terminated": {"exitCode": 0 if self.passed else 1,
                                     "containerID": container_id, "finishedAt": "2026-11-03T00:00:00Z"}} if terminal else {
                                         "running": {"startedAt": "2026-11-03T00:00:00Z"}}
            self.pods[name] = {"metadata": {"name": name + "-pod", "uid": name + "-pod-uid",
                                "namespace": obj["metadata"]["namespace"], "ownerReferences": [{
                                    "apiVersion": "batch/v1", "kind": "Job", "name": name,
                                    "uid": obj["metadata"]["uid"], "controller": True}]},
                              "spec": copy.deepcopy(obj["spec"]["template"]["spec"]),
                              "status": {"podIP": "10.0.0." + str(len(self.jobs)),
                                  "phase": ("Succeeded" if self.passed else "Failed") if terminal else "Running",
                                  "containerStatuses": [{"name": "run", "containerID": container_id,
                                                         "imageID": IMAGE, "restartCount": 0,
                                                         "ready": not terminal, "state": state}]}}
        return obj

    def get(self, kind, name, namespace=None):
        result = copy.deepcopy(self.jobs[name])
        if name == self.target and self.fault == "job_replaced" and self.logs:
            result["metadata"]["uid"] = "replacement-job"
        return result

    def call(self, *args, data=None):
        if args[0] == "wait":
            return ""
        if args[0] == "logs":
            self.logs.append(args)
            return json.dumps({"injected_value": self.passed, "missing_value_closed": self.passed})
        assert args[:2] == ("get", "pods")
        selector = args[args.index("-l") + 1].split("=", 1)[1]
        name = next(n for n, j in self.jobs.items() if j["metadata"]["uid"] == selector or n == selector)
        pod = copy.deepcopy(self.pods[name])
        status = pod["status"]["containerStatuses"][0]
        if name == self.target:
            if self.fault == "foreign_owner":
                pod["metadata"]["ownerReferences"][0]["uid"] = "unrelated-job"
            elif self.fault == "no_controller":
                pod["metadata"]["ownerReferences"][0]["controller"] = False
            elif self.fault == "pod_replaced" and self.logs:
                pod["metadata"]["uid"] = "replacement-pod"
            elif self.fault == "image_spec":
                pod["spec"]["containers"][0]["image"] = "registry.invalid/other:latest"
            elif self.fault == "image_runtime":
                status["imageID"] = "registry.invalid/python@sha256:" + "b" * 64
            elif self.fault == "restart":
                status["restartCount"] = 1
            elif self.fault == "container_replaced" and self.logs:
                status["containerID"] = "containerd://replacement"
            elif self.fault == "exit_status":
                status["state"] = {"terminated": {"exitCode": 137}}
            elif self.fault == "multiple":
                return json.dumps({"items": [pod, pod]})
        return json.dumps({"items": [pod]})

    def cleanup(self, namespace, uid):
        self.deleted.append((namespace, uid))
        return True


@pytest.mark.parametrize("passed", [True, False])
def test_job_execution_and_fixed_container_logs_prove_verdict(context, passed):
    kube = ExecutionKube(passed=passed)
    output = verification.verify_environment(kube, Proposal.model_validate(context[2]), IMAGE)
    assert output["failure_code"] == ("NONE" if passed else "TEMPLATE_TEST_FAILED")
    assert output["namespace_deleted"]
    assert len(output["executions"]) == 3
    assert kube.logs[0][:4] == ("logs", "pod/verify-pod", "-c", "run")
    assert output["executions"]["verify"]["exit_code"] == (0 if passed else 1)
    assert len(kube.deleted) == 1


@pytest.mark.parametrize("target", ["present", "missing", "verify"])
@pytest.mark.parametrize("fault", ["foreign_owner", "no_controller", "pod_replaced", "job_replaced",
                                   "multiple", "image_spec", "image_runtime", "restart",
                                   "container_replaced", "exit_status"])
def test_unbound_execution_is_unknown_and_cleanup_is_preserved(context, target, fault):
    bundle, snapshot, _, _ = context
    kube = ExecutionKube(fault, target)
    output = verification.integrate(bundle.model_dump(), snapshot.manifest.model_dump(), snapshot.root,
                                    result_for(context), kube, IMAGE)
    assert output["state"] == "NEEDS_HUMAN"
    assert output["receipt"]["verdict"] == "UNKNOWN"
    assert output["receipt"]["effects_known"] is False
    assert output["evidence"]["failure_code"] == "VERIFIER_UNAVAILABLE"
    assert output["evidence"]["namespace_deleted"]
    assert len(kube.deleted) == 1
