"""Local handoff checks only; no product Job or approval authority is simulated."""

from dataclasses import replace
from hashlib import sha256

import pytest

from rehearsal_runner.integration_handoff import prepare_handoff
from rehearsal_runner.isolation_gate import IsolationPass
from rehearsal_runner.runner import Namespace, ProposalRef


def inputs():
    return dict(
        proposal=ProposalRef("run-1", "a" * 40, "b" * 64),
        namespace=Namespace("rehearsal-test", "uid-1"),
        isolation=IsolationPass("rehearsal-test", "uid-1", "probe-1"),
        attempt=1, diff=b"candidate diff\n", image_digest="sha256:" + "c" * 64,
        target_environment_id="onprem",
    )


def test_handoff_preserves_isolation_identity_and_digests_exact_bytes():
    args = inputs()
    handoff = prepare_handoff(**args)
    assert handoff.namespace == args["namespace"]
    assert handoff.isolation_probe_run_id == "probe-1"
    assert handoff.diff_hash == "sha256:" + sha256(args["diff"]).hexdigest()
    assert handoff.image_digest == args["image_digest"]
    assert not hasattr(handoff, "verification_id")


@pytest.mark.parametrize("field,value", [
    ("isolation", IsolationPass("rehearsal-test", "uid-other", "probe-1")),
    ("isolation", IsolationPass("rehearsal-other", "uid-1", "probe-1")),
    ("isolation", IsolationPass("rehearsal-test", "uid-1", "")),
    ("attempt", 0), ("attempt", 4), ("attempt", True),
    ("diff", b""), ("image_digest", "latest"), ("target_environment_id", ""),
])
def test_invalid_or_mismatched_inputs_do_not_create_handoff(field, value):
    args = inputs()
    args[field] = value
    with pytest.raises(ValueError):
        prepare_handoff(**args)


def test_changed_diff_changes_binding():
    args = inputs()
    first = prepare_handoff(**args)
    second = prepare_handoff(**(args | {"diff": args["diff"] + b"!"}))
    assert first.diff_hash != second.diff_hash
    assert replace(first, diff_hash=second.diff_hash) == second
