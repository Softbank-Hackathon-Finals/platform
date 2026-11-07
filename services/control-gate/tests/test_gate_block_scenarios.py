"""T19 completion: an open bucket and a schema mismatch BLOCK, the fixed inputs PASS, the same inputs
always give the same decision. On the production assembly (gate_services) with operator signatures.

The T14 runs are the shared examples; the open bucket is IAM-01.1 ("can an unauthenticated client read
an attachment object?") allowed on the target, as in demo 1 of docs/controls/README.md.
"""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from bravo_platform_events import gate_readiness
from test_gate_assembly import gate  # noqa: F401
from test_readiness import ready  # noqa: F401

from bravo_gate.contract import canonical
from bravo_gate.models import RunSnapshot
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope

OPEN_BUCKET = {"verdict": "FAIL", "observed": "allow"}  # the anonymous GET succeeded


def target_run(f, name, **changes):
    doc = deepcopy(json.loads(f.state["runs"]["target"].raw))
    doc["run_id"] = name
    for row in doc["results"]:
        row.update(changes.get(row["test_id"], {}))
    return RunSnapshot(name, canonical(doc))


def gate_once(f, name, target, *, revision, schema=None):
    """Bind a context for `target`, capture T13 for it, sign the five inputs and evaluate."""
    ctx = replace(f.base, gate_id=name, target_ref=target.ref, target_digest=target.fingerprint)
    f.records.record_run(f.operator, f.plan.plan_id, "target", target)
    f.records.bind(f.operator, f.plan.plan_id, name, ctx, expected_revision=revision)
    f.state["context"] = ctx
    doc = deepcopy(f.doc)
    at, until = f.clock.at.isoformat(), (f.clock.at + timedelta(seconds=30)).isoformat()
    for part in [doc["source"], doc["target"], doc["replication"], *doc["sequences"]]:
        part.update(observed_at=at, expires_at=until)
    if schema:
        doc["target"]["tables"][0]["schema_hash"] = schema
    f.update(doc)
    gate_readiness.record_readiness(f.records, f.operator, f.plan.plan_id, f.data["binding"], f.data["snapshot"])
    for purpose, subject in (("context", ctx.fingerprint), ("run-source", ctx.source_digest),
                             ("run-target", ctx.target_digest), ("readiness-binding", f.data["binding"].fingerprint),
                             ("readiness-snapshot", f.data["snapshot"].fingerprint)):
        value = AuthorityEnvelope("operator-" + purpose, "key-one", purpose, ctx.fingerprint, subject,
                                  f.clock.at.isoformat(), (f.clock.at + timedelta(minutes=5)).isoformat(), "0" * 128)
        f.provenance.publish(f.issuer, f.plan.plan_id,
                             replace(value, signature_hex=f.keys[purpose].sign(DOMAIN + canonical(value.payload)).hex()))
    return f.evaluate()


def start(f):
    f.enroll()
    f.base = f.state["context"]
    f.clock.at = f.state["now"]
    f.records.record_run(f.operator, f.plan.plan_id, "source", f.state["runs"]["source"])


def test_an_open_bucket_blocks_and_the_fixed_target_passes(gate):  # noqa: F811
    f = gate
    start(f)
    report = gate_once(f, "gate-open-bucket", target_run(f, "target-open", **{"IAM-01.1": OPEN_BUCKET}), revision=0)
    assert report["decision"] == "BLOCK" and report["data"]["decision"] == "PASS"
    [row] = [r for r in report["security"]["rows"] if r["test_id"] == "IAM-01.1"]
    assert (row["source_verdict"], row["target_verdict"], row["reason"]) == ("PASS", "FAIL", "TARGET_FAIL")
    assert all(r["decision"] == "PASS" for r in report["security"]["rows"] if r["test_id"] != "IAM-01.1")
    # The same inputs give the same decision: evaluating again returns the identical report.
    assert f.evaluate() == report
    f.clock.at += timedelta(minutes=1)
    fixed = gate_once(f, "gate-bucket-fixed", target_run(f, "target-fixed"), revision=1)
    assert fixed["decision"] == "PASS"


def test_a_schema_mismatch_blocks_and_the_fixed_capture_passes(gate):  # noqa: F811
    f = gate
    start(f)
    target = target_run(f, "target-ok")
    report = gate_once(f, "gate-schema-mismatch", target, revision=0, schema="c" * 64)
    assert report["decision"] == "BLOCK" and report["security"]["decision"] == "PASS"
    blocked = {r["test_id"]: r["reason"] for r in report["data"]["rows"] if r["decision"] == "BLOCK"}
    assert blocked == {"public.messages:schema": "SCHEMA_MISMATCH"}
    assert f.evaluate() == report
    f.clock.at += timedelta(minutes=1)
    assert gate_once(f, "gate-schema-fixed", target, revision=1)["decision"] == "PASS"
