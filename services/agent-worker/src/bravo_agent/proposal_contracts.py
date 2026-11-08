"""Shared proposal/result validation without runtime or transport imports."""

import re

from .contracts import FailureBundle, Proposal, digest


def validate_result(result):
    """Validate broker output once at each external boundary using the same contract."""
    bundle = FailureBundle.model_validate(result["input"])
    proposal = Proposal.model_validate(result["proposal"])
    if (result.get("state") != "PROPOSAL_READY" or type(result.get("attempt")) is not int
            or result["attempt"] != 1 or result.get("input_digest") != digest(bundle)
            or result.get("proposal_digest") != digest(proposal)
            or result.get("run_id") != bundle.run_id or proposal.run_id != bundle.run_id
            or result.get("source_sha") != bundle.source_sha or proposal.source_sha != bundle.source_sha
            or not re.fullmatch(r"[a-f0-9]{64}", result.get("snapshot_digest", ""))):
        raise ValueError("unbound proposal result")
    return bundle, proposal


_RESPONSE_FIELDS = {"product_input_digest", "admission_digest", "runtime_source_sha", "state",
                    "input_kind", "run_id", "source_sha", "attempt", "input", "input_digest",
                    "task", "task_digest", "snapshot_digest", "proposal", "proposal_digest",
                    "tool_events_status"}


def validate_bound_product_response(result: dict, input_digest: str, runtime_source_sha: str) -> dict:
    """Validate the broker's projection; T15 separately binds it to its input.

    No verification verdict or model audit is reconstructed here. The runtime
    SHA is the exact reviewed installation selected by trusted deployment code.
    """
    if type(result) is not dict or set(result) - {"infra_plans"} != _RESPONSE_FIELDS:
        raise ValueError("invalid product response")
    if "infra_plans" in result and (type(result["infra_plans"]) is not list or not 1 <= len(result["infra_plans"]) <= 8
                                    or not all(type(p) is dict for p in result["infra_plans"])):
        raise ValueError("invalid product response")
    bundle, _ = validate_result(result)
    task = FailureBundle.model_validate(result["task"])
    if (result["input_kind"] != "product" or result["product_input_digest"] != input_digest
            or result["runtime_source_sha"] != runtime_source_sha
            or not isinstance(result["admission_digest"], str)
            or re.fullmatch(r"[a-f0-9]{64}", result["admission_digest"]) is None
            or task != bundle or result["task_digest"] != digest(task)
            or result["tool_events_status"] != "UNAVAILABLE"):
        raise ValueError("unbound product response")
    return result
