import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from bravo_controls.aws_identity import RegisteredRole
from bravo_controls.prowler_iam import (
    CHECKS,
    VERSION,
    InventoryUnavailable,
    RoleInventory,
    assess_iam_collection,
    read_role_inventory,
)
from bravo_controls.tool_reports import CheckMapping, ToolRun, assess_prowler

ROLE = RegisteredRole("arn:aws:iam::123456789012:role/app", "AROA" + "A" * 17)
DOCUMENT = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::fixture/*"}
    ],
}
TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        }
    ],
}
CANARY = "private-policy-marker-do-not-echo"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


INVENTORY = RoleInventory(ROLE, encoded(TRUST), (("app-only", encoded(DOCUMENT)),))
MAPPING = CheckMapping(
    "prowler",
    VERSION,
    "registered-revision-config",
    "candidate-1",
    tuple((c, ROLE.arn) for c in CHECKS),
)


def row(check, *, status="PASS"):
    return {
        "metadata": {"event_code": check, "product": {"uid": "prowler", "version": VERSION}},
        "resources": [
            {
                "uid": ROLE.arn,
                "data": {
                    "metadata": {
                        "arn": ROLE.arn,
                        "name": "app-only",
                        "type": "Inline",
                        "attached": True,
                        "entity": "app",
                        "document": DOCUMENT,
                    }
                },
            }
        ],
        "status": "New",
        "status_code": status,
    }


def run(rows=None):
    return ToolRun(
        "prowler",
        VERSION,
        MAPPING.scope,
        ROLE.arn,
        encoded([row(c) for c in CHECKS] if rows is None else rows),
        0,
        True,
    )


def assess(value=None, **overrides):
    args = dict(
        before=INVENTORY, after=INVENTORY, baseline_digest=INVENTORY.digest, diagnostics=b""
    )
    args.update(overrides)
    return assess_iam_collection(value or run(), MAPPING, **args)


def test_scanner_pass_with_caught_error_is_unknown_not_zero_violations():
    value = run()
    assert assess_prowler(value, MAPPING).observed == "absent"
    # Observed with the official Prowler 5.47.0 dependency set. Do not repair
    # upstream's result by deleting the diagnostic or choosing fewer checks.
    result = assess(
        value,
        diagnostics=b"Error checking privilege escalation for policy: InvalidActionPatternError",
    )
    assert result.observed is None and result.unknown_reason == "TOOL_ERROR"


def test_bound_clean_collection_is_only_aws_partial_assessment():
    result = assess()
    assert result.observed == "absent"
    assert result.summary == {"checks": 2, "violations": 0}
    failed = run([row(CHECKS[0], status="FAIL"), row(CHECKS[1])])
    assert assess(replace(failed, exit_code=3)).observed == "present"


@pytest.mark.parametrize("diagnostics", [None, "", b"warning", b"\n", CANARY.encode()])
def test_unconfirmed_or_nonempty_diagnostics_never_pass(diagnostics):
    result = assess(diagnostics=diagnostics)
    assert result.observed is None
    assert CANARY not in repr(result)


def test_incomplete_collection_and_baseline_drift_never_pass():
    assert assess(replace(run(), complete=False)).unknown_reason == "TOOL_ERROR"
    assert assess(baseline_digest="sha256:" + "0" * 64).observed is None
    changed = replace(INVENTORY, trust=encoded(DOCUMENT))
    assert assess(after=changed).observed is None
    assert assess(before=None).observed is None
    two = replace(INVENTORY, inline=INVENTORY.inline * 2)
    assert assess(before=two, after=two).unknown_reason == "NOT_RUN"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "extra",
        "document",
        "role",
        "name",
        "entity",
        "attached",
        "type",
        "metadata",
        "version",
        "muted",
    ],
)
def test_report_cannot_omit_or_replace_policy_analysis(change):
    rows = [row(c) for c in CHECKS]
    metadata = rows[0]["resources"][0]["data"]["metadata"]
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows[1] = rows[0]
    elif change == "extra":
        rows.append(row("extra-check"))
    elif change == "document":
        metadata["document"] = {}
    elif change == "role":
        rows[0]["resources"][0]["uid"] += "-other"
    elif change == "name":
        metadata["name"] = "omitted-policy"
    elif change == "entity":
        metadata["entity"] = "other"
    elif change == "attached":
        metadata["attached"] = False
    elif change == "type":
        metadata["type"] = "Custom"
    elif change == "metadata":
        del rows[0]["resources"][0]["data"]["metadata"]
    elif change == "version":
        rows[0]["metadata"]["product"]["version"] = "5.10.0"
    elif change == "muted":
        rows[0]["status"] = "Suppressed"
    assert assess(run(rows)).observed is None


def response(**kwargs):
    return {"ResponseMetadata": {"HTTPStatusCode": 200}, **kwargs}


def client():
    obj = Mock()
    obj.get_role.return_value = response(
        Role={
            "Arn": ROLE.arn,
            "RoleId": ROLE.role_id,
            "RoleName": "app",
            "AssumeRolePolicyDocument": TRUST,
        }
    )
    obj.list_role_policies.return_value = response(PolicyNames=["app-only"], IsTruncated=False)
    obj.list_attached_role_policies.return_value = response(AttachedPolicies=[], IsTruncated=False)
    obj.get_role_policy.return_value = response(
        RoleName="app", PolicyName="app-only", PolicyDocument=DOCUMENT
    )
    return obj


def test_independent_inventory_uses_only_get_list_and_rechecks_role_identity():
    obj = client()
    assert read_role_inventory(obj, ROLE) == INVENTORY
    assert [call[0] for call in obj.mock_calls] == [
        "get_role",
        "list_role_policies",
        "list_attached_role_policies",
        "get_role_policy",
        "get_role",
    ]
    assert ROLE.arn not in repr(INVENTORY)
    assert "s3:GetObject" not in repr(INVENTORY)


@pytest.mark.parametrize(
    "failure",
    [
        "missing-document",
        "empty-statement",
        "duplicate",
        "empty",
        "managed",
        "boundary",
        "role-recreated",
        "wrong-policy",
        "not-200",
        "missing-pagination",
        "repeated-marker",
        "overflow",
        "error",
        "role-changed",
    ],
)
def test_inventory_failure_is_fixed_error_without_provider_payload(failure):
    obj = client()
    if failure == "missing-document":
        del obj.get_role_policy.return_value["PolicyDocument"]
    elif failure == "empty-statement":
        obj.get_role_policy.return_value["PolicyDocument"] = {
            "Version": "2012-10-17",
            "Statement": [],
        }
    elif failure == "duplicate":
        obj.list_role_policies.return_value["PolicyNames"] *= 2
    elif failure == "empty":
        obj.list_role_policies.return_value["PolicyNames"] = []
    elif failure == "managed":
        obj.list_attached_role_policies.return_value["AttachedPolicies"] = [{"PolicyArn": CANARY}]
    elif failure == "boundary":
        obj.get_role.return_value["Role"]["PermissionsBoundary"] = {
            "PermissionsBoundaryArn": CANARY
        }
    elif failure == "role-recreated":
        obj.get_role.return_value["Role"]["RoleId"] = "AROA" + "B" * 17
    elif failure == "wrong-policy":
        obj.get_role_policy.return_value["PolicyName"] = CANARY
    elif failure == "not-200":
        obj.get_role_policy.return_value["ResponseMetadata"]["HTTPStatusCode"] = 403
    elif failure == "missing-pagination":
        del obj.list_role_policies.return_value["IsTruncated"]
    elif failure == "repeated-marker":
        obj.list_role_policies.return_value = response(
            PolicyNames=[], IsTruncated=True, Marker="repeat"
        )
    elif failure == "overflow":
        obj.list_role_policies.return_value["PolicyNames"] = [str(x) for x in range(21)]
    elif failure == "error":
        obj.get_role_policy.side_effect = RuntimeError(CANARY)
    elif failure == "role-changed":
        obj.get_role.side_effect = [
            obj.get_role.return_value,
            response(
                Role={
                    "Arn": ROLE.arn,
                    "RoleId": "AROA" + "B" * 17,
                    "RoleName": "app",
                    "AssumeRolePolicyDocument": TRUST,
                }
            ),
        ]
    with pytest.raises(InventoryUnavailable) as caught:
        read_role_inventory(obj, ROLE)
    assert CANARY not in str(caught.value)


def test_pagination_is_consumed_before_policy_document_reads():
    obj = client()
    obj.list_role_policies.side_effect = [
        response(PolicyNames=[], IsTruncated=True, Marker="next"),
        response(PolicyNames=["app-only"], IsTruncated=False),
    ]
    assert read_role_inventory(obj, ROLE) == INVENTORY
    assert obj.list_role_policies.call_args.kwargs["Marker"] == "next"
