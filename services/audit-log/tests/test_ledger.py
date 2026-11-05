import json

import jsonschema
import pytest

from bravo_audit.ledger import RunLedger, chain_digest, verify_chain
from bravo_audit.record import GENESIS, UnsafePayload, validate_record


def test_chain_is_deterministic_and_links_records(ledger, fresh_ledger):
    a = ledger.append("platform.events", "state.changed", {"to": "COLLECT"})
    b = ledger.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", {"category": "bucket_delete"})
    assert a.prev_sha256 == GENESIS and b.prev_sha256 == a.sha256
    assert (a.record_id, b.record_id) == ("run-demo-1:1", "run-demo-1:2")
    again = fresh_ledger()
    again.append("platform.events", "state.changed", {"to": "COLLECT"})
    again.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", {"category": "bucket_delete"})
    assert [r.sha256 for r in again.records] == [a.sha256, b.sha256]
    assert verify_chain([r.as_dict() for r in ledger.records]) == (True, "ok")
    assert chain_digest([r.as_dict() for r in ledger.records]) == b.sha256


@pytest.mark.parametrize("tamper", ["payload", "reorder", "drop", "relink", "sequence", "other_run"])
def test_any_change_after_the_fact_breaks_verification(ledger, tamper):
    for i in range(3):
        ledger.append("executor", "execution.updated", {"step": f"s{i}", "status": "succeeded"})
    docs = [json.loads(json.dumps(r.as_dict())) for r in ledger.records]
    if tamper == "payload":
        docs[1]["payload"]["status"] = "failed"
    elif tamper == "reorder":
        docs[1], docs[2] = docs[2], docs[1]
    elif tamper == "drop":
        del docs[1]
    elif tamper == "relink":
        docs[2]["prev_sha256"] = docs[0]["sha256"]
    elif tamper == "sequence":
        docs[2]["sequence"] = 5
    else:
        docs[2]["run_id"] = "run-demo-2"
    ok, reason = verify_chain(docs)
    assert ok is False and reason.split(":")[0] in {"invalid_record", "sequence_gap", "chain_broken", "mixed_runs"}


@pytest.mark.parametrize("payload", [
    {"note": "AKIA" + "A" * 16},
    {"dsn": "postgresql://chat:hunter2hunter2@db/chat"},
    {"k": "password = 'hunter2hunter2'"},
    {"pem": "-----BEGIN RSA PRIVATE KEY-----"},
    {"big": "x" * (16 * 1024 + 1)},
])
def test_credential_like_payloads_are_refused_before_hashing(ledger, payload):
    with pytest.raises(UnsafePayload):
        ledger.append("gate", "gate.completed", payload)
    assert ledger.records == [] and ledger.next_sequence == 1


def test_resume_continues_a_verified_chain(ledger, fresh_ledger):
    ledger.append("platform.events", "state.changed", {"to": "COLLECT"})
    stored = [r.as_dict() for r in ledger.records]
    resumed = RunLedger.resume("plan-demo", "run-demo-1", stored, now=fresh_ledger().now)
    nxt = resumed.append("platform.events", "state.changed", {"to": "ANALYZE"})
    assert nxt.sequence == 2 and nxt.prev_sha256 == stored[0]["sha256"]
    assert verify_chain(stored + [nxt.as_dict()]) == (True, "ok")
    stored[0]["payload"]["to"] = "DONE"
    with pytest.raises(ValueError):
        RunLedger.resume("plan-demo", "run-demo-1", stored)
    with pytest.raises(ValueError):
        RunLedger.resume("plan-other", "run-demo-1", [r.as_dict() for r in ledger.records])


def test_validate_record_rejects_unknown_fields_and_sources(ledger):
    doc = ledger.append("gate", "gate.completed", {"decision": "PASS"}).as_dict()
    doc["extra"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validate_record(doc)
    del doc["extra"]
    doc["source"] = "model"
    with pytest.raises(jsonschema.ValidationError):
        validate_record(doc)
