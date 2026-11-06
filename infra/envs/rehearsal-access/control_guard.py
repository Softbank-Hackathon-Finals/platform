"""Render the namespace-local admission boundary for T15's writer Jobs.

Operator identities are explicit installation input; no RBAC or credentials are granted.
"""
import argparse
import json
import re
from pathlib import Path

NAMESPACE = "bravo-rehearsal-control"
NAME = "t15-control-creators"
TOKEN_SECRETS = "t15-control-no-token-secrets"
RUNNER = f"system:serviceaccount:{NAMESPACE}:t15-runner"
JOB_CONTROLLER = "system:serviceaccount:kube-system:job-controller"
GARBAGE_COLLECTOR = "system:serviceaccount:kube-system:generic-garbage-collector"


def render(operators):
    if not isinstance(operators, list) or not operators:
        raise ValueError("explicit operator identities required")
    for user in operators:
        if not isinstance(user, str) or not user or user != user.strip() or len(user) > 253:
            raise ValueError("invalid operator identity")
        if user.startswith("system:") and user != "system:admin" and not re.fullmatch(
                r"system:serviceaccount:bravo-team-access:member-[0-9]{2}", user):
            raise ValueError("only named personal test accounts may be operator exceptions")
        if any(ord(c) < 32 for c in user):
            raise ValueError("invalid operator identity")
    if len(set(operators)) != len(operators):
        raise ValueError("duplicate operator identity")
    # JSON string literals are valid CEL literals. No source-text interpolation of identities.
    allowed = json.dumps(operators + [RUNNER])
    expression = (
        f"request.userInfo.username in {allowed} || "
        f"(request.userInfo.username in {json.dumps([JOB_CONTROLLER, GARBAGE_COLLECTOR])} && "
        "request.operation == 'UPDATE' && object.spec == oldObject.spec) || "
        f"(request.userInfo.username == {json.dumps(JOB_CONTROLLER)} && "
        "request.resource.group == '' && request.resource.resource == 'pods' && "
        "has(object.metadata.ownerReferences) && "
        "object.metadata.ownerReferences.exists(o, o.apiVersion == 'batch/v1' && "
        "o.kind == 'Job' && has(o.controller) && o.controller && size(o.uid) > 0))"
    )
    policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": NAME},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                "resourceRules": [
                    {"apiGroups": [""], "apiVersions": ["v1"],
                     "operations": ["CREATE", "UPDATE"], "resources": ["pods"], "scope": "Namespaced"},
                    {"apiGroups": ["batch"], "apiVersions": ["v1"],
                     "operations": ["CREATE", "UPDATE"], "resources": ["jobs"], "scope": "Namespaced"},
                ],
            },
            "validations": [{
                "expression": expression,
                "message": "T15 control workloads require the runner, an approved personal operator, "
                           "or the Job controller creating a Job-owned Pod",
                "reason": "Forbidden",
            }],
        },
    }
    binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": NAME},
        "spec": {"policyName": NAME, "validationActions": ["Deny"]},
    }
    # #12 review: a Secret of type service-account-token is filled with a non-expiring token,
    # outside the 600 s exec-token bound and Secret-deletion revocation. The runner needs other
    # Secrets (per-attempt writer inputs), so only this type is refused, for every creator.
    token_policy = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": TOKEN_SECRETS},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                "resourceRules": [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                                   "resources": ["secrets"], "scope": "Namespaced"}],
            },
            "validations": [{
                "expression": "!has(object.type) || object.type != 'kubernetes.io/service-account-token'",
                "message": "non-expiring service-account token Secrets are not allowed in the T15 control namespace",
                "reason": "Forbidden",
            }],
        },
    }
    token_binding = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": TOKEN_SECRETS},
        "spec": {"policyName": TOKEN_SECRETS, "validationActions": ["Deny"]},
    }
    return {"apiVersion": "v1", "kind": "List", "items": [policy, binding, token_policy, token_binding]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operators", type=Path, required=True,
                        help="private JSON array of approved human/operator usernames")
    args = parser.parse_args()
    print(json.dumps(render(json.loads(args.operators.read_text())), indent=2))


if __name__ == "__main__":
    main()

