import asyncio
import copy

import pytest

from bravo_agent import verification
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
