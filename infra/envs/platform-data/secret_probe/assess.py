"""Match each observed GET to a trusted Kubernetes API audit event, without exporting raw events."""

import argparse
import json
from pathlib import Path
from uuid import UUID


def classify(receipt, events):
    ns = receipt["namespace"]
    roles = ("reader", "denied", "reader")
    steps = receipt["steps"]
    if len(steps) != 3 or not receipt["cleanup_confirmed"]:
        return "UNKNOWN"
    ids = [step.get("audit_id") for step in steps]
    if len(set(ids)) != 3 or any(not value or str(UUID(value)) != value for value in ids):
        return "UNKNOWN"
    matched = {}
    for event in events:
        aid = event.get("auditID")
        if aid not in ids or event.get("stage") != "ResponseComplete":
            continue
        if aid in matched:
            return "UNKNOWN"
        matched[aid] = event
    statuses = []
    for role, step in zip(roles, steps, strict=True):
        status = step["status"]
        obj = matched.get(step["audit_id"], {})
        user = obj.get("user", {})
        ref = obj.get("objectRef", {})
        if (obj.get("verb") != "get" or "impersonatedUser" in obj or ref.get("apiGroup", "")
                or ref.get("subresource") or ref.get("resource") != "secrets"
                or ref.get("name") != "t7-synthetic" or ref.get("namespace") != ns
                or user.get("username") != f"system:serviceaccount:{ns}:{role}"
                or not user.get("uid") or user["uid"] != receipt["service_accounts"][role]
                or user.get("extra", {}).get("authentication.kubernetes.io/pod-uid") != [step["pod_uid"]]
                or obj.get("responseStatus", {}).get("code") != status):
            return "UNKNOWN"
        decision = obj.get("annotations", {}).get("authorization.k8s.io/decision")
        if status == 200:
            if decision != "allow" or step.get("uid") != receipt["fixture_uid"]:
                return "UNKNOWN"
        elif status != 403 or decision != "forbid":
            return "UNKNOWN"
        statuses.append(status)
    if statuses == [200, 200, 200]:
        return "FAIL"
    return "PASS" if statuses == [200, 403, 200] else "UNKNOWN"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("audit_jsonl", type=Path)
    args = p.parse_args()
    try:
        with args.audit_jsonl.open() as audit:
            verdict = classify(json.loads(args.receipt.read_text()), (json.loads(line) for line in audit))
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        verdict = "UNKNOWN"
    print(verdict)
    raise SystemExit({"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[verdict])
