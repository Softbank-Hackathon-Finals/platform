import json
from dataclasses import dataclass

import pytest

from rehearsal_runner.attempt_store import AttemptStore, RecordUnavailable, _Trace, judge_attempt
from rehearsal_runner.bucket_job import BucketEvidence
from rehearsal_runner.runner import AttemptResult, Namespace, ProposalRef

KEY = b"k" * 32


@dataclass(frozen=True)
class Admission:
    plan_id: str = "plan-1"
    tenant_id: str = "tenant-1"
    plan_version: int = 1
    task_id: str = "bucket"
    rehearsal_id: str = "run-1"
    attempt: int = 1
    source_sha: str = "a" * 40
    task_digest: str = "b" * 64


def test_claims_an_attempt_once_and_records_once(tmp_path):
    store = AttemptStore(tmp_path, KEY)
    admission = Admission()
    assert store.claim(admission) is True
    assert store.claim(admission) is False
    record = store.write(admission, {"verdict": "NEEDS_HUMAN", "reason": "X"})
    assert record["verification_id"].startswith("verification-")
    assert store.read("run-1", 1) == record
    with pytest.raises(RecordUnavailable):
        store.write(admission, {"verdict": "PASS"})


def test_unclaimed_or_other_admission_cannot_be_recorded(tmp_path):
    store = AttemptStore(tmp_path, KEY)
    with pytest.raises(RecordUnavailable):
        store.write(Admission(), {"verdict": "PASS"})
    store.claim(Admission())
    with pytest.raises(RecordUnavailable):
        store.write(Admission(plan_version=2), {"verdict": "PASS"})


@pytest.mark.parametrize("change", ["tamper", "key"])
def test_altered_record_or_other_key_is_rejected(tmp_path, change):
    store = AttemptStore(tmp_path, KEY)
    store.claim(Admission())
    store.write(Admission(), {"verdict": "FAIL"})
    if change == "tamper":
        path = tmp_path / "run-1" / "1" / "result.json"
        raw = json.loads(path.read_text())
        raw["record"]["verdict"] = "PASS"
        path.write_text(json.dumps(raw))
    else:
        store = AttemptStore(tmp_path, b"o" * 32)
    with pytest.raises(RecordUnavailable):
        store.read("run-1", 1)


def test_rejects_short_key_and_invalid_attempt(tmp_path):
    with pytest.raises(ValueError):
        AttemptStore(tmp_path, b"short")
    with pytest.raises(ValueError):
        AttemptStore(tmp_path, KEY).read("run-1", 4)


def evidence(verdict="PASS", name="rehearsal-a", uid="uid-1", digest="c" * 64):
    return BucketEvidence(verdict, "NONE", name, uid, digest, "sha256:" + "d" * 64,
                          "sha256:" + "e" * 64, {}, {}, b"diff\n")


NAMESPACE = Namespace("rehearsal-a", "uid-1")


def traced(created=NAMESPACE, removed="uid-1", probe="probe-1"):
    return _Trace(created, removed, probe)


def result(receipt, state="RECEIPT_UNVERIFIED", reason="RECEIPT_REQUIRES_VALIDATION"):
    return AttemptResult(state, reason, receipt, ProposalRef("run-1", "a" * 40, "c" * 64))


def test_binds_verdict_only_with_matching_identities():
    body = judge_attempt(result(evidence()), traced())
    assert body["verdict"] == "PASS" and body["created_uid"] == body["removed_uid"] == "uid-1"
    assert body["diff"] == "diff\n" and body["isolation_probe_run_id"] == "probe-1"
    assert judge_attempt(result(evidence("FAIL")), traced())["verdict"] == "FAIL"


@pytest.mark.parametrize("receipt,trace", [
    (evidence(uid="uid-2"), traced()),
    (evidence(name="rehearsal-b"), traced()),
    (evidence(digest="f" * 64), traced()),
    (evidence(), traced(removed=None)),
    (evidence(), traced(probe=None)),
    ({"verdict": "PASS"}, traced()),
])
def test_unbound_evidence_needs_a_human(receipt, trace):
    assert judge_attempt(result(receipt), trace)["verdict"] == "NEEDS_HUMAN"


def test_blocked_or_unknown_attempt_needs_a_human():
    body = judge_attempt(AttemptResult("BLOCK", "ISOLATION_BLOCKED"), traced())
    assert body == {"verdict": "NEEDS_HUMAN", "reason": "ISOLATION_BLOCKED"}


def test_unknown_effects_keep_the_verifier_failure_code():
    trace = traced()
    trace.failure = "job_timeout"
    body = judge_attempt(AttemptResult("NEEDS_HUMAN", "EFFECTS_UNKNOWN"), trace)
    assert body == {"verdict": "NEEDS_HUMAN", "reason": "EFFECTS_UNKNOWN", "detail": "job_timeout"}
