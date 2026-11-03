import asyncio
import copy
import importlib.util
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from bravo_agent.contracts import digest
from bravo_agent.coordinator import Coordinator

spec = importlib.util.spec_from_file_location(
    "subscription_controller", Path(__file__).resolve().parents[3] / "operations/subscription/controller.py")
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def denied(operation):
    return ClientError({"Error": {"Code": "AccessDeniedException"}}, operation)


def test_only_start_denial_proves_session_boundary():
    class SSM:
        def start_session(self, **kwargs):
            raise denied("StartSession")

    assert controller.prove_session_denied(SSM(), Target="synthetic")


def test_cleanup_denial_cannot_be_mistaken_for_start_denial():
    class SSM:
        def start_session(self, **kwargs):
            return {"SessionId": "synthetic"}

        def terminate_session(self, **kwargs):
            raise denied("TerminateSession")

    with pytest.raises(ClientError):
        controller.prove_session_denied(SSM(), Target="synthetic")


def test_accepted_negative_session_is_closed_and_rejected():
    closed = []

    class SSM:
        def start_session(self, **kwargs):
            return {"SessionId": "synthetic"}

        def terminate_session(self, **kwargs):
            closed.append(kwargs["SessionId"])

    with pytest.raises(RuntimeError, match="boundary"):
        controller.prove_session_denied(SSM(), Target="synthetic")
    assert closed == ["synthetic"]


def response_pair(context):
    bundle, snapshot, proposal, _ = context

    class Provider:
        async def propose(self, bundle, tools):
            tools.dispatch("mcp__bravo__plan_propose", proposal)

    result = asyncio.run(Coordinator(bundle, snapshot).propose(Provider()))
    evidence = {"namespace_deleted": True, "job_complete": True, "failure_code": "NONE",
                "checks": {"injected_value": True, "missing_value_closed": True}}
    receipt = {"run_id": bundle.run_id, "source_sha": bundle.source_sha,
               "proposal_digest": result["proposal_digest"], "attempt": 1,
               "verdict": "PASS", "effects_known": True, "evidence_digest": digest(evidence)}
    return {"result": result, "state": "PROPOSAL_READY", "active_after": True, "exit_code": 0,
            "runtime_source_sha": "a" * 40, "credential_rotated": False, "registration_reused": True}, {
                "state": "AWAITING_APPROVAL", "receipt": receipt, "evidence": evidence}


def test_controller_accepts_only_matching_pass(context):
    broker, verification = response_pair(context)
    controller.accept_verification(broker, verification)


@pytest.mark.parametrize("field,value", [
    ("run_id", "previous-run"), ("source_sha", "b" * 40), ("proposal_digest", "b" * 64),
    ("attempt", 2), ("verdict", "UNKNOWN"), ("effects_known", False), ("evidence_digest", "c" * 64),
])
def test_controller_rejects_receipt_mismatch(context, field, value):
    broker, verification = response_pair(context)
    verification["receipt"][field] = value
    with pytest.raises(ValueError):
        controller.accept_verification(broker, verification)


@pytest.mark.parametrize("field", ["run_id", "source_sha", "proposal_digest", "attempt", "verdict",
                                  "effects_known", "evidence_digest"])
def test_controller_rejects_missing_receipt_fields(context, field):
    broker, verification = response_pair(context)
    del verification["receipt"][field]
    with pytest.raises(ValueError):
        controller.accept_verification(broker, verification)


@pytest.mark.parametrize("field,value", [("namespace_deleted", False), ("job_complete", False),
                                         ("failure_code", "VERIFIER_UNAVAILABLE"), ("checks", {})])
def test_controller_rejects_invalid_evidence_even_with_recomputed_digest(context, field, value):
    broker, verification = response_pair(context)
    verification["evidence"][field] = value
    verification["receipt"]["evidence_digest"] = digest(verification["evidence"])
    with pytest.raises(ValueError):
        controller.accept_verification(broker, verification)


def test_controller_rejects_tampered_evidence(context):
    broker, verification = response_pair(context)
    verification["evidence"]["extra"] = "changed"
    with pytest.raises(ValueError):
        controller.accept_verification(broker, verification)


@pytest.mark.parametrize("replay", [False, True])
def test_main_binds_ssm_to_current_request_and_rejects_previous_receipt(context, monkeypatch, tmp_path, replay):
    broker, verification = response_pair(context)
    calls, received, receipts = [], [], []
    ids = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    monkeypatch.chdir(tmp_path)
    for key, value in {"BRAVO_INSTANCE_ID": "synthetic", "BRAVO_RUNTIME_SOURCE_SHA": "a" * 40,
                       "ACTIONS_ID_TOKEN_REQUEST_URL": "https://synthetic.invalid/token?x=1",
                       "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic"}.items():
        monkeypatch.setenv(key, value)

    def open_url(request, **kwargs):
        if request.data is None:
            data = {"value": "synthetic"}
        else:
            run_id = json.loads(request.data).get("run_id", "subscription-" + ("a" if not received else "b") * 32)
            data = copy.deepcopy(broker)
            result = data["result"]
            for entry in (result, result["input"], result["proposal"]):
                entry["run_id"] = run_id
            result["input_digest"] = digest(result["input"])
            result["proposal_digest"] = digest(result["proposal"])
            receipt = copy.deepcopy(verification)
            receipt["receipt"].update(run_id=run_id, proposal_digest=result["proposal_digest"])
            receipts.append(receipt)
            received.append(data)
        return io.BytesIO(json.dumps(data).encode())

    class SSM:
        def send_command(self, **kwargs):
            calls.append(kwargs)
            return {"Command": {"CommandId": "command"}}

        def get_waiter(self, *args):
            return SimpleNamespace(wait=lambda **kwargs: None)

        def start_session(self, **kwargs):
            return {"SessionId": "session"}

        def terminate_session(self, **kwargs):
            pass

        def get_command_invocation(self, **kwargs):
            receipt = receipts[0] if replay else receipts[-1]
            return {"StandardOutputContent": json.dumps(receipt)}

    monkeypatch.setattr(controller, "prove_denied", lambda call: True)
    monkeypatch.setattr(controller, "prove_session_denied", lambda *args, **kwargs: True)
    monkeypatch.setattr(controller.boto3, "client", lambda *args, **kwargs: SSM())
    monkeypatch.setattr(controller.urllib.request, "urlopen", open_url)
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: io.BytesIO())
    monkeypatch.setattr(controller.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        terminate=lambda: None, wait=lambda **kwargs: None))
    if replay:
        with pytest.raises(RuntimeError, match="verification did not pass"):
            controller.main()
    else:
        controller.main()
    verifies = [c["Parameters"] for c in calls if c["Parameters"]["operation"] == ["verify"]]
    assert len(verifies) == 2
    for params, data in zip(verifies, received, strict=True):
        assert params["runId"] == [data["result"]["run_id"]]
        assert params["proposalDigest"] == [data["result"]["proposal_digest"]]
    assert calls[-1]["Parameters"] == {"operation": ["stop"]}
