import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "infra/envs/rehearsal-access/control_guard.py"
SPEC = importlib.util.spec_from_file_location("control_guard", PATH)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


@pytest.mark.parametrize("users", [[], None, [None], [{}], [" alice"], ["a\nb"],
                                  ["alice", "alice"], ["system:masters"],
                                  ["system:serviceaccount:cnpg-system:cnpg-cloudnative-pg"],
                                  [guard.JOB_CONTROLLER], [guard.RUNNER]])
def test_cannot_turn_workload_or_controller_into_operator_exception(users):
    with pytest.raises(ValueError):
        guard.render(users)


def test_personal_test_identity_exception_is_explicit_and_does_not_grant_rbac():
    result = guard.render(["system:admin", "operator@example.invalid",
                           "system:serviceaccount:bravo-team-access:member-01"])
    assert {x["kind"] for x in result["items"]} == {
        "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
    assert result["items"][1]["spec"]["validationActions"] == ["Deny"]
