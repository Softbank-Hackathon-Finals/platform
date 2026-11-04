from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from bravo_gate.combined import CombinedGateService
from bravo_gate.contract import GateError, canonical, fingerprint, time
from bravo_gate.models import RunSnapshot, VerifiedAuthority
from bravo_gate.readiness import ReadinessBinding, ReadinessPolicy, assess_readiness


@pytest.fixture
def ready(service):
    security, state = service
    ctx, now = state["context"], state["now"]
    policy = ReadinessPolicy("demo.v1", "registered-schema-rule.v1",
                             ("public.messages", "public.attachments"),
                             (("public.messages", "id"),), 0, 60)
    doc = {"kind": "data-readiness.v1", "context_digest": ctx.fingerprint,
           "policy_digest": policy.fingerprint, "sequences": [{
               "table": "public.messages", "column": "id", "source_next": 10,
               "target_next": 20, "target_max_id": 99, "proposed_next": 100,
               "readback_next": 100, "increment": 1, "cycle": False,
               "observed_at": now.isoformat(), "expires_at": (now + timedelta(seconds=30)).isoformat()}]}
    for side in ("source", "target"):
        doc[side] = {"environment_id": getattr(ctx, side + "_environment_id"),
                     "environment_ref": getattr(ctx, side + "_environment_ref"),
                     "database_ref": "registered-db-" + side, "schema_rule": policy.schema_rule,
                     "observed_at": now.isoformat(), "expires_at": (now + timedelta(seconds=30)).isoformat(),
                     "tables": [{"name": t, "schema_hash": "b" * 64, "primary_key": ["id"]}
                                for t in policy.tables]}
    doc["replication"] = {"source_database_ref": "registered-db-source", "target_database_ref": "registered-db-target",
                          "lag_bytes": 0, "observed_at": now.isoformat(),
                          "expires_at": (now + timedelta(seconds=30)).isoformat()}
    data = {"policy": policy, "trusted": True}

    def update(value):
        value = deepcopy(value)
        value["context_digest"] = state["context"].fingerprint
        data["snapshot"] = RunSnapshot("readiness-ref", canonical(value))
        data["binding"] = ReadinessBinding(state["context"].fingerprint, data["policy"],
                                           "registered-db-source", "registered-db-target",
                                           "readiness-ref", fingerprint(value))

    update(doc)
    app = CombinedGateService(security=security, binding_provider=lambda *_: data["binding"],
                              snapshot_provider=lambda *_: data["snapshot"],
                              authenticate_binding=lambda *_: data["trusted"],
                              authenticate_snapshot=lambda *_: data["trusted"])
    return app, state, data, doc, update


def test_security_and_readiness_pass_does_not_authorize_execution(ready):
    app, _, _, _, _ = ready
    report = app.evaluate("actor", "context")
    assert report["decision"] == "PASS"
    assert app.evaluate("actor", "context") == report
    permit = app.require_prerequisites("actor", "context", canonical(report))
    assert not permit.authorizes_cutover
    assert permit.report_digest == fingerprint(report)


@pytest.mark.parametrize("change", ["schema", "missing_table", "missing_sequence", "lag", "pk", "unknown", "sequence", "rule", "stale", "future"])
def test_security_pass_cannot_hide_data_failure(ready, change):
    app, _, _, doc, update = ready
    if change == "schema":
        doc["target"]["tables"][0]["schema_hash"] = "c" * 64
    elif change == "missing_table":
        doc["target"]["tables"].pop()
    elif change == "missing_sequence":
        doc["sequences"].clear()
    elif change == "lag":
        doc["replication"]["lag_bytes"] = 1
    elif change == "pk":
        doc["source"]["tables"][0]["primary_key"] = []
    elif change == "unknown":
        doc["target"]["tables"][0]["schema_hash"] = None
    elif change == "sequence":
        doc["sequences"][0]["readback_next"] = 1
    elif change == "rule":
        doc["source"]["schema_rule"] = "other-rule"
    elif change == "stale":
        doc["target"]["expires_at"] = doc["target"]["observed_at"]
    elif change == "future":
        doc["source"]["observed_at"] = doc["source"]["expires_at"]
    update(doc)
    report = app.evaluate("actor", "context")
    assert report["security"]["decision"] == "PASS"
    assert report["decision"] == report["data"]["decision"] == "BLOCK"
    report["decision"] = "PASS"
    with pytest.raises(GateError):
        app.require_prerequisites("actor", "context", canonical(report))


def test_open_bucket_blocks_then_fix_and_same_input_passes(ready):
    app, state, _, doc, update = ready
    original = state["runs"]["target"].document
    changed = deepcopy(original)
    changed["results"][0].update(verdict="FAIL", observed="allow")
    for value, expected in [(changed, "BLOCK"), (original, "PASS")]:
        state["runs"]["target"] = RunSnapshot(state["context"].target_ref, canonical(value))
        state["context"] = replace(state["context"], target_digest=fingerprint(value))
        update(doc)
        report = app.evaluate("actor", "context")
        assert report["decision"] == expected
        assert report == app.evaluate("actor", "context")


def test_schema_mismatch_fix_requires_new_binding(ready):
    app, _, _, doc, update = ready
    first = app.evaluate("actor", "context")
    doc["target"]["tables"][0]["schema_hash"] = "c" * 64
    update(doc)
    assert app.evaluate("actor", "context")["decision"] == "BLOCK"
    with pytest.raises(GateError, match="CONTEXT_MISMATCH"):
        app.require_prerequisites("actor", "context", canonical(first))
    doc["target"]["tables"][0]["schema_hash"] = "b" * 64
    doc["target"]["expires_at"] = doc["source"]["observed_at"].replace(":00+", ":20+")
    update(doc)
    assert app.evaluate("actor", "context")["decision"] == "PASS"


@pytest.mark.parametrize("change", ["duplicate", "reversed_db", "environment", "context", "policy", "extra"])
def test_bad_scopes_rejected(ready, change):
    app, _, data, doc, update = ready
    if change == "duplicate":
        doc["target"]["tables"].append(deepcopy(doc["target"]["tables"][0]))
    elif change == "reversed_db":
        doc["replication"]["source_database_ref"] = "registered-db-target"
    elif change == "environment":
        doc["source"]["environment_ref"] = "another-environment"
    elif change == "policy":
        doc["policy_digest"] = "f" * 64
    elif change == "extra":
        doc["PASS"] = True
    update(doc)
    if change == "context":
        data["binding"] = replace(data["binding"], context_digest="f" * 64)
    with pytest.raises(GateError):
        app.evaluate("actor", "context")


@pytest.mark.parametrize("field,value", [("lag_bytes", True), ("lag_bytes", -1), ("lag_bytes", None)])
def test_unknown_replication_is_not_zero(ready, field, value):
    app, _, _, doc, update = ready
    doc["replication"][field] = value
    update(doc)
    assert app.evaluate("actor", "context")["decision"] == "BLOCK"


@pytest.mark.parametrize("changes", [{"proposed_next": 20, "readback_next": 20},
                                     {"cycle": True}, {"increment": -1},
                                     {"readback_next": True}, {"source_next": None}])
def test_sequence_maximum_and_nonstandard_cases(ready, changes):
    app, _, _, doc, update = ready
    doc["sequences"][0].update(changes)
    update(doc)
    assert app.evaluate("actor", "context")["decision"] == "BLOCK"


def test_expiry_revocation_and_permission_rechecked_on_consumption(ready):
    app, state, data, _, _ = ready
    report = canonical(app.evaluate("actor", "context"))
    for field in ("trusted", "allowed"):
        store = data if field == "trusted" else state
        store[field] = False
        with pytest.raises(GateError):
            app.require_prerequisites("actor", "context", report)
        store[field] = True
    state["now"] += timedelta(seconds=30)
    with pytest.raises(GateError):
        app.require_prerequisites("actor", "context", report)


def test_authority_expiry_limits_combined_prerequisite(ready):
    app, state, _, _, _ = ready
    expires = state["now"] + timedelta(seconds=1)
    app.authenticate_snapshot = lambda _, ctx, binding, snapshot: VerifiedAuthority(
        ctx.fingerprint, snapshot.fingerprint, "readiness-snapshot", expires)
    report = app.evaluate("actor", "context")
    permit = app.require_prerequisites("actor", "context", canonical(report))
    assert time(permit.valid_until) == expires


def test_provider_failure_redacted_and_changes_during_read_rejected(ready):
    app, _, data, _, _ = ready
    def unavailable(*_):
        raise RuntimeError("credential=must-not-leak")
    app.snapshot_provider = unavailable
    with pytest.raises(GateError, match="^AUTHORITY_UNAVAILABLE$"):
        app.evaluate("actor", "context")
    app.snapshot_provider = lambda *_: data["snapshot"]
    count = 0
    def changing(*_):
        nonlocal count
        count += 1
        return data["binding"] if count == 1 else replace(data["binding"], snapshot_ref="different")
    app.binding_provider = changing
    with pytest.raises(GateError):
        app.evaluate("actor", "context")


def test_data_snapshot_and_policy_are_immutable(ready):
    _, state, data, _, _ = ready
    document = data["snapshot"].document
    document["target"]["tables"].clear()
    assert assess_readiness(state["context"], data["binding"], data["snapshot"], now=state["now"])["decision"] == "PASS"


def test_pinned_contract_matches_merged_repository():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    package = Path(__file__).resolve().parents[1] / "src/bravo_gate/contracts"
    for name in ("control-result.schema.json", "profiles.json"):
        assert (package / name).read_bytes() == (root / "docs/controls" / name).read_bytes()
