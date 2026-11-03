import importlib.util
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

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
