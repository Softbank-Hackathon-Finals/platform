import json

import jsonschema
import pytest

from bravo_audit.ledger import OriginConflict, RunLedger, chain_digest, verify_chain
from bravo_audit.record import GENESIS, UnsafePayload, validate_record


def test_chain_is_deterministic_and_links_records(ledger, fresh_ledger):
    a, _ = ledger.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1")
    b, _ = ledger.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", {"category": "bucket_delete"}, "t11:run-demo-1:attempt1:0")
    assert a.prev_sha256 == GENESIS and b.prev_sha256 == a.sha256
    assert (a.record_id, b.record_id) == ("run-demo-1:1", "run-demo-1:2")
    again = fresh_ledger()
    again.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1")
    again.append("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", {"category": "bucket_delete"}, "t11:run-demo-1:attempt1:0")
    assert [r.sha256 for r in again.records] == [a.sha256, b.sha256]
    assert verify_chain([r.as_dict() for r in ledger.records]) == (True, "ok")
    assert chain_digest([r.as_dict() for r in ledger.records]) == b.sha256


@pytest.mark.parametrize("tamper", ["payload", "reorder", "drop", "relink", "sequence", "other_run"])
def test_any_change_after_the_fact_breaks_verification(ledger, tamper):
    for i in range(3):
        ledger.append("executor", "execution.updated", {"step": f"s{i}", "status": "succeeded"}, f"t12:exec:{i}")
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
    # review (#257): structured credential fields, at any depth, with or without an assignment shape
    {"data": {"password": "synthetic-sensitive-value"}},
    {"data": {"api_key": "synthetic-sensitive-value"}},
    {"credentials": {"nested": {"secret_key": "synthetic-sensitive-value"}}},
    {"DB_PASSWORD": "synthetic-sensitive-value"},
    {"items": [{"authorization": "Bearer synthetic"}]},
    # review round 2: lists under a sensitive key, and "<" prefixes that are not the exact marker
    {"credentials": ["synthetic-sensitive-value"]},
    {"credentials": [{"secret_id": "demo/ref", "value": "synthetic-sensitive-value"}]},  # pointer next to a value: still refused
    {"password": {"password_ref": {"inner_password": "synthetic-sensitive-value"}}},
    # review (#257, 류지환 4차): a reference-named key exempts only a string, never a subtree
    {"password": {"password_ref": {"value": "synthetic-sensitive-value"}}},
    {"credentials": {"password_ref": ["synthetic-sensitive-value"]}},
    {"credentials": {"context_name": {"token": "synthetic-sensitive-value"}}},
    {"credentials": {"context_name": ["synthetic-sensitive-value"]}},
    {"credentials": {"list": [{"inner": "synthetic-sensitive-value"}]}},
    {"password": "<synthetic-sensitive-value>"},
    {"password": "<masked but with extra text that is not the marker form!!>"},
])
def test_credential_like_payloads_are_refused_before_hashing(ledger, payload):
    with pytest.raises(UnsafePayload):
        ledger.append("gate", "gate.completed", payload, "t19:x")
    assert ledger.records == [] and ledger.next_sequence == 1


@pytest.mark.parametrize("payload", [
    {"env_sources": {"DB_PASSWORD": "secretKeyRef", "ADMIN_TOKEN": "secretKeyRef"}},   # where a value comes from
    {"secret_id": "demo-chat/db", "secret_arn": "arn:aws:secretsmanager:::x.example.invalid"},  # references
    {"token_count": 3, "password": None, "api_key_enabled": True},                       # non-values
    {"snippet": "DB_SECRET_ID: <masked>", "password_source": "openbao"},
    {"password": "<masked>", "api_key": "<masked: secret-like literal>", "token": "<redacted>"},
    # review (#257, 류지환 3차): reference-named fields stay exempt inside sensitive containers
    {"credentials": [{"secret_id": "demo/ref"}]},
    {"credentials": [{"password_source": "openbao"}]},
    {"password": {"password_ref": "demo/ref/password"}},
    {"credentials": {"secret_arn": "arn:aws:secretsmanager:::x.example.invalid", "rotation_enabled": True}},
])
def test_credential_references_without_values_are_recordable(ledger, payload):
    record, created = ledger.append("platform.events", "state.changed", payload, "t10:plan-demo:9")
    assert created and record.payload == payload


def test_redelivered_upstream_event_is_absorbed_and_conflict_refused(ledger, fresh_ledger):
    first, created = ledger.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1")
    again, created_again = ledger.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1")
    assert created and not created_again and again is first and len(ledger.records) == 1
    with pytest.raises(OriginConflict):
        ledger.append("platform.events", "state.changed", {"to": "ANALYZE"}, "t10:plan-demo:1")
    assert len(ledger.records) == 1 and ledger.next_sequence == 2
    # the same rules hold after a resume from stored records
    resumed = RunLedger.resume("plan-demo", "run-demo-1", [r.as_dict() for r in ledger.records], now=fresh_ledger().now)
    assert resumed.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1") == (None, False)
    with pytest.raises(OriginConflict):
        resumed.append("platform.events", "state.changed", {"to": "ANALYZE"}, "t10:plan-demo:1")
    nxt, created = resumed.append("platform.events", "state.changed", {"to": "ANALYZE"}, "t10:plan-demo:2")
    assert created and nxt.sequence == 2


def test_resume_continues_a_verified_chain(ledger, fresh_ledger):
    ledger.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1")
    stored = [r.as_dict() for r in ledger.records]
    resumed = RunLedger.resume("plan-demo", "run-demo-1", stored, now=fresh_ledger().now)
    nxt, _ = resumed.append("platform.events", "state.changed", {"to": "ANALYZE"}, "t10:plan-demo:2")
    assert nxt.sequence == 2 and nxt.prev_sha256 == stored[0]["sha256"]
    assert verify_chain(stored + [nxt.as_dict()]) == (True, "ok")
    stored[0]["payload"]["to"] = "DONE"
    with pytest.raises(ValueError):
        RunLedger.resume("plan-demo", "run-demo-1", stored)
    with pytest.raises(ValueError):
        RunLedger.resume("plan-other", "run-demo-1", [r.as_dict() for r in ledger.records])


def test_validate_record_rejects_unknown_fields_and_sources(ledger):
    doc = ledger.append("gate", "gate.completed", {"decision": "PASS"}, "t19:gate-1")[0].as_dict()
    doc["extra"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validate_record(doc)
    del doc["extra"]
    doc["source"] = "model"
    with pytest.raises(jsonschema.ValidationError):
        validate_record(doc)


def test_concurrent_redelivery_of_one_origin_creates_one_record(fresh_ledger):
    import threading

    ledger = fresh_ledger()
    results = []

    def worker():
        results.append(ledger.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for _, created in results if created) == 1 and len(ledger.records) == 1 and ledger.next_sequence == 2


def test_chain_digest_survives_a_resume_that_only_absorbs_duplicates(ledger, fresh_ledger):
    ledger.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1")
    stored = [r.as_dict() for r in ledger.records]
    resumed = RunLedger.resume("plan-demo", "run-demo-1", stored, now=fresh_ledger().now)
    assert resumed.append("platform.events", "state.changed", {"to": "COLLECT"}, "t10:plan-demo:1") == (None, False)
    assert resumed.chain_digest() == stored[-1]["sha256"] == chain_digest(stored)
    assert RunLedger("plan-demo", "run-demo-1").chain_digest() == chain_digest([])
