"""T5 semantic evaluator. Report production is not authorization or attestation."""

from collections import Counter
from datetime import timedelta

from .contract import GateError, time
from .models import StoredException


def exception_valid(record, ctx, test_id, now, contract):
    if not isinstance(record, StoredException):
        return False
    body = record.body
    try:
        validator = contract.validator.evolve(
            schema={"$ref": "#/$defs/ExceptionApproval", "$defs": contract.schema["$defs"]}
        )
        if not validator.is_valid(body):
            return False
        return (
            not record.revoked
            and record.context_digest == ctx.fingerprint
            and body["scope"]
            == {
                "app": ctx.app,
                "target_environment_id": ctx.target_environment_id,
                "test_id": test_id,
                "profile_id": ctx.profile_id,
                "profile_version": ctx.profile_version,
                "context_digest": ctx.fingerprint,
            }
            and bool(body["approved_by"].strip())
            and bool(body["compensating_control"].strip())
            and bool(body["reason"].strip())
            and time(body["approved_at"]) <= now < time(body["expires_at"])
        )
    except GateError:
        return False


def _reason(a, b, spec, now):
    for row in (a, b):
        known = row["verdict"] in {"PASS", "FAIL"}
        if (
            row["control_id"] != spec["control_id"]
            or row["expected"] != spec["expected"]
            or (
                spec["positive_control"] == "required"
                and row["positive_control"] == "not_applicable"
            )
            or (
                known
                and spec["positive_control"] == "required"
                and row["positive_control"] != "pass"
            )
            or (
                spec["positive_control"] == "not_applicable"
                and row["positive_control"] != "not_applicable"
            )
            or (known and (row["observed"] == row["expected"]) != (row["verdict"] == "PASS"))
        ):
            return "INVARIANT_VIOLATION"
    if any(r["method_version"] != spec["method_version"] for r in (a, b)):
        return "METHOD_MISMATCH"
    for row in (a, b):
        proof = row["evidence"]
        if proof is not None and not time(proof["collected_at"]) <= now < time(proof["expires_at"]):
            return "EVIDENCE_EXPIRED"
    if a["verdict"] == "UNKNOWN":
        return "SOURCE_UNKNOWN"
    if b["verdict"] == "UNKNOWN":
        return "TARGET_UNKNOWN"
    if b["verdict"] == "FAIL":
        return "TARGET_FAIL"
    return "IMPROVED" if a["verdict"] == "FAIL" else "BOTH_PASS"


def evaluate(contract, ctx, source, target, *, now, approvals=(), network_verified=None):
    """Trusted caller selects the profile/context and authenticates all inputs.

    ctx.app is the registered workload (a T10 workload_id). The catalog profile's
    app names only its reference app, so the profile is pinned by the plan, not here.
    """
    profile = contract.profile(ctx.profile_id, ctx.profile_version)
    if ctx.catalog_digest != contract.catalog_digest:
        raise GateError("PROFILE_CONTEXT_MISMATCH")
    for run in (source, target):
        contract.validate(run, "control_run")
        if not time(run["started_at"]) <= time(run["finished_at"]) <= now:
            raise GateError("INVALID_RUN_TIME")
        for row in run["results"]:
            proof = row["evidence"]
            if proof is not None and not time(run["started_at"]) <= time(
                proof["collected_at"]
            ) <= time(run["finished_at"]):
                raise GateError("EVIDENCE_OUTSIDE_RUN")
    issues = []
    if source["app"] != ctx.app or target["app"] != ctx.app:
        issues.append("APP_MISMATCH")
    if source["environment"]["role"] != "source" or target["environment"]["role"] != "target":
        issues.append("ENVIRONMENT_ROLE_MISMATCH")
    if source["environment"]["id"] == target["environment"]["id"]:
        issues.append("SAME_ENVIRONMENT")
    counts = [Counter(r["test_id"] for r in run["results"]) for run in (source, target)]
    by_id = [{r["test_id"]: r for r in run["results"]} for run in (source, target)]
    if type(approvals) is not tuple or any(not isinstance(r, StoredException) for r in approvals):
        raise GateError("SERVER_APPROVALS_REQUIRED")
    ids = [r.body.get("approval_id") for r in approvals]
    if len(ids) != len(set(ids)):
        raise GateError("DUPLICATE_APPROVAL_ID")
    rows = []
    for test_id in profile["required_test_ids"]:
        a, b = (v.get(test_id) for v in by_id)
        reason = "BOTH_PASS"
        if any(c[test_id] == 0 for c in counts):
            reason = "MISSING_RESULT"
        elif any(c[test_id] != 1 for c in counts):
            reason = "DUPLICATE_RESULT"
        else:
            reason = _reason(a, b, contract.catalog["tests"][test_id], now)
        if reason in {"BOTH_PASS", "IMPROVED", "TARGET_FAIL"} and test_id in {
            "NET-01.2",
            "NET-01.3",
        }:
            # A timeout string, positive_control="pass" or configuration dump is
            # insufficient by itself. The host verifies a bound structured proof.
            for side, row in [("source", a), ("target", b)]:
                if (
                    row["verdict"] == "PASS"
                    and row["observed"] == "deny"
                    and (
                        network_verified is None or network_verified(side, test_id, row) is not True
                    )
                ):
                    reason = "SOURCE_UNKNOWN" if side == "source" else "TARGET_UNKNOWN"
                    break
        decision = "PASS" if reason in {"BOTH_PASS", "IMPROVED"} else "BLOCK"
        exception = None
        if reason == "TARGET_FAIL" and not issues:
            candidates = [
                r
                for r in approvals
                if isinstance(r.body.get("scope"), dict)
                and {k: v for k, v in r.body["scope"].items() if k != "context_digest"}
                == {
                    "app": ctx.app,
                    "target_environment_id": ctx.target_environment_id,
                    "test_id": test_id,
                    "profile_id": ctx.profile_id,
                    "profile_version": ctx.profile_version,
                }
            ]
            if candidates:
                if len(candidates) == 1 and exception_valid(
                    candidates[0], ctx, test_id, now, contract
                ):
                    exception = candidates[0].body
                    decision = "EXCEPTION"
                    reason = "EXCEPTION_APPROVED"
                else:
                    reason = "EXCEPTION_INVALID"
        rows.append(
            {
                "test_id": test_id,
                "control_id": contract.catalog["tests"][test_id]["control_id"],
                "question": test_id,
                "source_verdict": a["verdict"] if a else "UNKNOWN",
                "target_verdict": b["verdict"] if b else "UNKNOWN",
                "decision": decision,
                "reason": reason,
                "improved": reason == "IMPROVED",
                "exception": exception,
            }
        )
    report = {
        "kind": "gate_comparison",
        "schema_version": "1",
        "gate_id": ctx.gate_id,
        "context_digest": ctx.fingerprint,
        "profile": {"id": ctx.profile_id, "version": ctx.profile_version},
        "app": ctx.app,
        "evaluated_at": now.isoformat().replace("+00:00", "Z"),
        "source_run_id": source["run_id"],
        "target_run_id": target["run_id"],
        "source_environment_id": source["environment"]["id"],
        "target_environment_id": target["environment"]["id"],
        "run_issues": issues,
        "decision": "BLOCK" if issues or any(r["decision"] == "BLOCK" for r in rows) else "PASS",
        "rows": rows,
    }
    contract.validate(report, "gate_comparison")
    return report


def consume(contract, ctx, document, source, target, *, now, approvals=(), network_verified=None):
    """Recompute from trusted current inputs. JSON PASS is never a bearer grant."""
    contract.validate(document, "gate_comparison")
    if document["context_digest"] != ctx.fingerprint or document["gate_id"] != ctx.gate_id:
        raise GateError("GATE_CONTEXT_MISMATCH")
    profile = contract.profile(ctx.profile_id, ctx.profile_version)
    ids = [r["test_id"] for r in document["rows"]]
    if len(ids) != len(set(ids)) or set(ids) != set(profile["required_test_ids"]):
        raise GateError("ROWS_NOT_MATCH_PROFILE")
    if not now - timedelta(seconds=60) <= time(document["evaluated_at"]) <= now:
        raise GateError("GATE_STALE_OR_FUTURE")
    for row in document["rows"]:
        if row["exception"] is not None:
            matching = [a for a in approvals if a.body == row["exception"]]
            if (
                len(matching) != 1
                or not exception_valid(matching[0], ctx, row["test_id"], now, contract)
                or not exception_valid(
                    matching[0], ctx, row["test_id"], time(document["evaluated_at"]), contract
                )
            ):
                raise GateError("EXCEPTION_INVALID")
    if any(time(run["finished_at"]) > time(document["evaluated_at"]) for run in (source, target)):
        raise GateError("RUN_FOLLOWS_GATE_EVALUATION")
    expected = evaluate(
        contract,
        ctx,
        source,
        target,
        now=now,
        approvals=approvals,
        network_verified=network_verified,
    )

    # Questions are presentation. All authorization-relevant fields are compared.
    def relevant(value):
        return {
            **{k: v for k, v in value.items() if k not in {"evaluated_at", "rows"}},
            "rows": sorted(
                ({k: v for k, v in row.items() if k != "question"} for row in value["rows"]),
                key=lambda r: r["test_id"],
            ),
        }

    if relevant(document) != relevant(expected):
        raise GateError("GATE_RECOMPUTATION_MISMATCH")
    if expected["decision"] != "PASS":
        raise GateError("SECURITY_GATE_BLOCKED")
    return expected
