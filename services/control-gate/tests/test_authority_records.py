"""Actual SQLite/PG locks and Ed25519; explicit synthetic registered identities."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.plan_api import Principal
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_readiness import ready  # noqa: F401
from test_stored_services import stored, stored_postgres  # noqa: F401

from bravo_gate.authority_records import AuthoritySubject, PlanAuthorityRecords, _envelope_ref
from bravo_gate.contract import GateError, canonical, time
from bravo_gate.models import RunSnapshot
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope, AuthorityKey

PURPOSES = (
    "context",
    "run-source",
    "run-target",
    "readiness-binding",
    "readiness-snapshot",
    "gate-result",
)


@pytest.fixture
def registry(stored):  # noqa: F811
    f = stored
    ctx = f.state["context"]
    manager = Principal("fixture-key-manager", ctx.tenant_id)
    producer = Principal("fixture-producer", ctx.tenant_id)
    grants = {
        manager.subject_id: {"gate:manage-provenance"},
        producer.subject_id: {"gate:publish-provenance", "gate:read"},
        f.principal.subject_id: {
            "gate:read",
            "gate:record",
            "gate:bind",
            "gate:evaluate",
            "gate:consume",
        },
    }
    connections = []

    def authorize(c, principal, plan, action):
        connections.append(c)
        return (
            f.allowed
            and principal.tenant_id == ctx.tenant_id
            and plan["plan_id"] == f.plan.plan_id
            and action in grants.get(principal.subject_id, set())
        )

    f.records = GateRecords(f.store, authorize=authorize)
    f.connections = connections
    clock = {"now": f.state["now"] - timedelta(hours=3)}

    def external(view, purpose, subject):
        assert view.c is connections[-1]
        known = {
            "readiness-binding": f.data["binding"].fingerprint,
            "readiness-snapshot": f.data["snapshot"].fingerprint,
        }
        if known.get(purpose) != subject:
            return None
        return AuthoritySubject(ctx.fingerprint, subject, purpose, f.doc["source"]["observed_at"])

    ledger = PlanAuthorityRecords(
        f.records, clock=lambda: clock["now"], external_subject_provider=external
    )
    fixture = SimpleNamespace(
        f=f,
        ledger=ledger,
        manager=manager,
        producer=producer,
        grants=grants,
        clock=clock,
        keys={},
        public={},
        enrolled={},
        connections=connections,
    )

    def enroll(purpose):
        private = Ed25519PrivateKey.generate()
        key = AuthorityKey(
            private.public_key().public_bytes_raw().hex(),
            (ctx.tenant_id,),
            (purpose,),
            (f.state["now"] - timedelta(hours=4)).isoformat(),
            (f.state["now"] + timedelta(hours=1)).isoformat(),
            False,
        )
        fixture.keys[purpose] = private
        fixture.public[purpose] = key
        fixture.enrolled[purpose] = ledger.enroll(
            manager,
            f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id=purpose,
            producer_subject_id=producer.subject_id,
            key=key,
            expected_revision=0,
        )
        return fixture.enrolled[purpose]

    fixture.enroll = enroll
    for purpose in PURPOSES:
        enroll(purpose)
    clock["now"] = f.state["now"]
    f.prepare()

    def envelope(selected_purpose, subject, **changes):
        body = (
            dict(
                issuer_id="fixture-issuer",
                key_id=selected_purpose,
                purpose=selected_purpose,
                context_digest=ctx.fingerprint,
                subject_digest=subject,
                issued_at=clock["now"].isoformat(),
                expires_at=(clock["now"] + timedelta(seconds=30)).isoformat(),
            )
            | changes
        )
        return AuthorityEnvelope(
            **body,
            signature_hex=fixture.keys[selected_purpose].sign(DOMAIN + canonical(body)).hex(),
        )

    fixture.envelope = envelope
    fixture.publish = lambda purpose, subject, **changes: ledger.publish(
        producer, f.plan.plan_id, envelope(purpose, subject, **changes)
    )

    def view(callback, principal=None):
        who = principal or f.principal
        with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
            row = dict(
                c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone()
            )
            return callback(f.records.view(c, who, row))

    fixture.view = view
    return fixture


def test_enrollment_is_idempotent_and_has_no_private_material(registry):
    r = registry
    old = r.enrolled["context"]
    assert (
        r.ledger.enroll(
            r.manager,
            r.f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id="context",
            producer_subject_id=r.producer.subject_id,
            key=r.public["context"],
            expected_revision=0,
        )
        == old
    )
    assert set(old["key"]) == set(asdict(r.public["context"]))
    assert old["revision"] == 1 and old["enrolled_by"] == r.manager.subject_id


def test_locked_publication_and_current_signature_readback(registry):
    r = registry
    ctx = r.f.state["context"]
    first = r.publish("context", ctx.fingerprint)
    assert r.publish("context", ctx.fingerprint) == first
    proof = r.view(lambda view: r.ledger.authority(view).verify(ctx, ctx.fingerprint, "context"))
    assert proof.purpose == "context" and proof.subject_digest == ctx.fingerprint
    assert proof.valid_until == r.clock["now"] + timedelta(seconds=30)


def test_complete_stored_services_use_current_registry_on_one_plan_connection(registry):
    r = registry
    f = r.f
    ctx = f.state["context"]
    for purpose, subject in [
        ("context", ctx.fingerprint),
        ("run-source", f.state["runs"]["source"].fingerprint),
        ("run-target", f.state["runs"]["target"].fingerprint),
        ("readiness-binding", f.data["binding"].fingerprint),
        ("readiness-snapshot", f.data["snapshot"].fingerprint),
    ]:
        r.publish(purpose, subject)
    f.factory.authority_provider = r.ledger.authority
    before = f.store.snapshot(f.plan.plan_id)
    report = f.evaluate()
    assert report["decision"] == "PASS"
    security = RunSnapshot(ctx.gate_id, canonical(report["security"]))
    r.publish("gate-result", security.fingerprint)

    def consume(view):
        proof = f.factory(view).require_prerequisites(
            f.principal.subject_id, view.context_id, canonical(report)
        )
        assert not proof.authorizes_cutover

    r.view(consume)
    r.ledger.revoke(
        r.manager,
        f.plan.plan_id,
        issuer_id="fixture-issuer",
        key_id="run-source",
        expected_revision=1,
    )
    with pytest.raises(GateError, match="REVOKED_OR_EXPIRED"):
        r.view(consume)
    assert f.store.snapshot(f.plan.plan_id) == before


def test_revocation_is_append_only_idempotent_and_key_id_cannot_be_reenrolled(registry):
    r = registry
    ctx = r.f.state["context"]
    r.publish("context", ctx.fingerprint)
    revoked = r.ledger.revoke(
        r.manager,
        r.f.plan.plan_id,
        issuer_id="fixture-issuer",
        key_id="context",
        expected_revision=1,
    )
    assert revoked["revision"] == 2 and revoked["key"]["revoked"] is True
    assert (
        r.ledger.revoke(
            r.manager,
            r.f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id="context",
            expected_revision=1,
        )
        == revoked
    )
    with pytest.raises(GateError, match="REVOKED_OR_EXPIRED"):
        r.view(lambda view: r.ledger.authority(view).verify(ctx, ctx.fingerprint, "context"))
    with pytest.raises(GateError, match="AUTHORITY_KEY_CONFLICT"):
        r.ledger.enroll(
            r.manager,
            r.f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id="context",
            producer_subject_id=r.producer.subject_id,
            key=r.public["context"],
            expected_revision=0,
        )


@pytest.mark.parametrize(
    "fault", ["producer-as-manager", "reader-as-publisher", "other-tenant", "grant-revoked"]
)
def test_permissions_are_separate_and_live(registry, fault):
    r = registry
    principal = r.producer if fault == "producer-as-manager" else r.f.principal
    if fault == "other-tenant":
        principal = replace(r.producer, tenant_id="other-tenant")
    if fault == "grant-revoked":
        principal = r.producer
        r.grants[r.producer.subject_id].clear()
    with pytest.raises(GateError):
        if fault == "producer-as-manager":
            r.ledger.revoke(
                principal,
                r.f.plan.plan_id,
                issuer_id="fixture-issuer",
                key_id="context",
                expected_revision=1,
            )
        else:
            r.ledger.publish(
                principal, r.f.plan.plan_id, r.envelope("context", r.f.state["context"].fingerprint)
            )


@pytest.mark.parametrize(
    "fault",
    [
        "self-enroll",
        "broad-purpose",
        "other-tenant",
        "already-revoked",
        "expired",
        "boolean-revision",
        "key-alias",
    ],
)
def test_enrollment_refuses_ambiguous_scope_reuse_and_bad_revision(registry, fault):
    r = registry
    key = AuthorityKey(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex(),
        (r.manager.tenant_id,),
        ("context",),
        (r.clock["now"] - timedelta(seconds=1)).isoformat(),
        (r.clock["now"] + timedelta(seconds=60)).isoformat(),
        False,
    )
    subject = r.producer.subject_id
    revision = 0
    if fault == "self-enroll":
        subject = r.manager.subject_id
    if fault == "broad-purpose":
        key = replace(key, purposes=("context", "gate-result"))
    if fault == "other-tenant":
        key = replace(key, tenants=("other-tenant",))
    if fault == "already-revoked":
        key = replace(key, revoked=True)
    if fault == "expired":
        key = replace(
            key,
            valid_from=(r.clock["now"] - timedelta(seconds=2)).isoformat(),
            valid_until=(r.clock["now"] - timedelta(seconds=1)).isoformat(),
        )
    if fault == "boolean-revision":
        revision = False
    if fault == "key-alias":
        key = r.public["context"]
    with pytest.raises(GateError):
        r.ledger.enroll(
            r.manager,
            r.f.plan.plan_id,
            issuer_id="another-issuer",
            key_id="another-key",
            producer_subject_id=subject,
            key=key,
            expected_revision=revision,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "wrong-producer",
        "wrong-key",
        "purpose",
        "context",
        "subject",
        "signature",
        "expired",
        "future",
        "before-enrollment",
    ],
)
def test_publication_cannot_register_invalid_or_retroactive_authority(registry, fault):
    r = registry
    ctx = r.f.state["context"]
    subject = ctx.fingerprint
    changes = {}
    actor = r.producer
    if fault == "wrong-producer":
        actor = r.f.principal
        r.grants[actor.subject_id].add("gate:publish-provenance")
    if fault == "wrong-key":
        changes["key_id"] = "run-source"
    if fault == "purpose":
        changes["purpose"] = "gate-result"
    if fault == "context":
        changes["context_digest"] = "f" * 64
    if fault == "subject":
        subject = "f" * 64
    if fault == "expired":
        changes.update(
            issued_at=(r.clock["now"] - timedelta(seconds=2)).isoformat(),
            expires_at=(r.clock["now"] - timedelta(seconds=1)).isoformat(),
        )
    if fault == "future":
        changes["issued_at"] = (r.clock["now"] + timedelta(seconds=1)).isoformat()
    if fault == "before-enrollment":
        changes["issued_at"] = (
            time(r.enrolled["context"]["enrolled_at"]) - timedelta(seconds=1)
        ).isoformat()
    value = r.envelope("context", subject, **changes)
    if fault == "signature":
        value = replace(value, signature_hex="0" * 128)
    with pytest.raises(GateError):
        r.ledger.publish(actor, r.f.plan.plan_id, value)


def test_collected_run_cannot_be_signed_under_a_key_enrolled_after_collection(registry):
    r = registry
    r.clock["now"] += timedelta(seconds=1)
    private = Ed25519PrivateKey.generate()
    key = replace(
        r.public["run-source"], public_key_hex=private.public_key().public_bytes_raw().hex()
    )
    r.ledger.enroll(
        r.manager,
        r.f.plan.plan_id,
        issuer_id="fixture-late",
        key_id="late-key",
        producer_subject_id=r.producer.subject_id,
        key=key,
        expected_revision=0,
    )
    body = dict(
        issuer_id="fixture-late",
        key_id="late-key",
        purpose="run-source",
        context_digest=r.f.state["context"].fingerprint,
        subject_digest=r.f.state["runs"]["source"].fingerprint,
        issued_at=r.clock["now"].isoformat(),
        expires_at=(r.clock["now"] + timedelta(seconds=20)).isoformat(),
    )
    envelope = AuthorityEnvelope(**body, signature_hex=private.sign(DOMAIN + canonical(body)).hex())
    with pytest.raises(GateError, match="SUBJECT_PREDATES_AUTHORITY_ENROLLMENT"):
        r.ledger.publish(r.producer, r.f.plan.plan_id, envelope)


def test_external_subject_is_explicit_and_provider_diagnostics_stay_private(registry):
    r = registry
    r.ledger.external = None
    with pytest.raises(GateError, match="EXTERNAL_AUTHORITY_SUBJECT_REQUIRED"):
        r.publish("readiness-binding", r.f.data["binding"].fingerprint)

    def broken(*_):
        raise RuntimeError("password=fixture-private-value")

    r.ledger.external = broken
    with pytest.raises(GateError) as error:
        r.publish("readiness-binding", r.f.data["binding"].fingerprint)
    assert str(error.value) == "AUTHORITY_UNAVAILABLE" and "private-value" not in str(error.value)


def test_unknown_view_or_missing_records_is_not_authority(registry):
    with pytest.raises(GateError, match="SERVER_RECORD_VIEW_REQUIRED"):
        registry.ledger.authority(SimpleNamespace())
    with pytest.raises(GateError):
        PlanAuthorityRecords(None, clock=lambda: registry.clock["now"])


def test_valid_signature_cannot_use_a_key_for_another_purpose(registry):
    r = registry
    value = r.envelope("context", r.f.state["runs"]["source"].fingerprint, purpose="run-source")
    with pytest.raises(GateError, match="REVOKED_OR_EXPIRED"):
        r.ledger.publish(r.producer, r.f.plan.plan_id, value)


def test_conflicting_concurrent_envelopes_never_overwrite_each_other(registry):
    r = registry
    ctx = r.f.state["context"]
    values = [
        r.envelope("context", ctx.fingerprint),
        r.envelope(
            "context",
            ctx.fingerprint,
            expires_at=(r.clock["now"] + timedelta(seconds=29)).isoformat(),
        ),
    ]

    def publish(value):
        try:
            return r.ledger.publish(r.producer, r.f.plan.plan_id, value)
        except GateError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [
            future.result(timeout=10)
            for future in [workers.submit(publish, value) for value in values]
        ]
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count("AUTHORITY_ENVELOPE_CONFLICT") == 1
    assert (
        r.view(
            lambda view: r.ledger.authority(view).verify(ctx, ctx.fingerprint, "context")
        ).purpose
        == "context"
    )


@pytest.mark.parametrize("fault", ["expired-before-commit", "publisher-revoked-before-commit"])
def test_publication_rolls_back_when_current_authority_is_lost(registry, monkeypatch, fault):
    r = registry
    ctx = r.f.state["context"]
    insert = r.ledger._insert

    def lose_current_authority(*args):
        insert(*args)
        if fault == "expired-before-commit":
            r.clock["now"] += timedelta(seconds=31)
        else:
            r.grants[r.producer.subject_id].discard("gate:publish-provenance")

    monkeypatch.setattr(r.ledger, "_insert", lose_current_authority)
    with pytest.raises(GateError):
        r.publish("context", ctx.fingerprint)
    r.clock["now"] = r.f.state["now"]
    assert (
        r.view(
            lambda view: r.ledger.authority(view).envelope_provider(ctx, ctx.fingerprint, "context")
        )
        is None
    )


@pytest.mark.parametrize(
    "fault", ["boolean", "dict", "context", "subject", "purpose", "before-trust", "future"]
)
def test_external_subject_metadata_cannot_be_unbound_or_retroactive(registry, fault):
    r = registry
    ctx = r.f.state["context"]
    value = AuthoritySubject(
        ctx.fingerprint,
        r.f.data["binding"].fingerprint,
        "readiness-binding",
        r.clock["now"].isoformat(),
    )
    if fault == "boolean":
        value = True
    if fault == "dict":
        value = asdict(value)
    if fault == "context":
        value = replace(value, context_digest="f" * 64)
    if fault == "subject":
        value = replace(value, subject_digest="f" * 64)
    if fault == "purpose":
        value = replace(value, purpose="readiness-snapshot")
    if fault == "before-trust":
        value = replace(
            value,
            observed_at=(
                time(r.enrolled["readiness-binding"]["enrolled_at"]) - timedelta(seconds=1)
            ).isoformat(),
        )
    if fault == "future":
        value = replace(value, observed_at=(r.clock["now"] + timedelta(seconds=1)).isoformat())
    r.ledger.external = lambda *_: value
    with pytest.raises(GateError):
        r.publish("readiness-binding", r.f.data["binding"].fingerprint)


@pytest.mark.parametrize(
    "fault",
    [
        "kind",
        "context",
        "subject",
        "purpose",
        "publisher",
        "future-publication",
        "early-publication",
        "extra",
        "envelope-extra",
    ],
)
def test_a_stored_signature_with_inconsistent_metadata_is_not_authority(registry, fault):
    r = registry
    ctx = r.f.state["context"]
    envelope = r.envelope("context", ctx.fingerprint)
    value = dict(
        kind="t19-authority-envelope.v1",
        context_digest=ctx.fingerprint,
        subject_digest=ctx.fingerprint,
        purpose="context",
        envelope=asdict(envelope),
        published_at=r.clock["now"].isoformat(),
        published_by=r.producer.subject_id,
    )
    if fault == "kind":
        value["kind"] = "unregistered"
    if fault == "context":
        value["context_digest"] = "f" * 64
    if fault == "subject":
        value["subject_digest"] = "f" * 64
    if fault == "purpose":
        value["purpose"] = "run-source"
    if fault == "publisher":
        value["published_by"] = r.manager.subject_id
    if fault == "future-publication":
        value["published_at"] = (r.clock["now"] + timedelta(seconds=1)).isoformat()
    if fault == "early-publication":
        value["published_at"] = (r.clock["now"] - timedelta(seconds=1)).isoformat()
    if fault == "extra":
        value["approved"] = True
    if fault == "envelope-extra":
        value["envelope"]["approved"] = True
    with r.f.store._transaction(write=True, plan_id=r.f.plan.plan_id) as c:
        c.execute(
            "INSERT INTO plan_commands VALUES (?,?,?,?)",
            (
                r.f.plan.plan_id,
                _envelope_ref(ctx, ctx.fingerprint, "context"),
                "{}",
                canonical(value).decode(),
            ),
        )
    with pytest.raises(GateError):
        r.view(lambda view: r.ledger.authority(view).verify(ctx, ctx.fingerprint, "context"))
    with pytest.raises(GateError):
        r.ledger.publish(r.producer, r.f.plan.plan_id, envelope)


def test_signature_authority_cannot_outlive_its_plan_transaction(registry):
    r = registry
    ctx = r.f.state["context"]
    r.publish("context", ctx.fingerprint)
    cached = r.view(r.ledger.authority)
    with pytest.raises(GateError, match="AUTHORITY_VIEW_UNAVAILABLE"):
        cached.verify(ctx, ctx.fingerprint, "context")


def test_pre_enrolled_key_with_later_valid_from_cannot_attest_earlier_run(registry):
    r = registry
    ctx = r.f.state["context"]
    r.clock["now"] = time(r.enrolled["run-source"]["enrolled_at"])
    private = Ed25519PrivateKey.generate()
    key = replace(
        r.public["run-source"],
        public_key_hex=private.public_key().public_bytes_raw().hex(),
        valid_from=(r.f.state["now"] - timedelta(hours=1)).isoformat(),
    )
    r.ledger.enroll(
        r.manager,
        r.f.plan.plan_id,
        issuer_id="fixture-future-validity",
        key_id="future-run-key",
        producer_subject_id=r.producer.subject_id,
        key=key,
        expected_revision=0,
    )
    r.clock["now"] = r.f.state["now"]
    body = dict(
        issuer_id="fixture-future-validity",
        key_id="future-run-key",
        purpose="run-source",
        context_digest=ctx.fingerprint,
        subject_digest=r.f.state["runs"]["source"].fingerprint,
        issued_at=r.clock["now"].isoformat(),
        expires_at=(r.clock["now"] + timedelta(seconds=30)).isoformat(),
    )
    value = AuthorityEnvelope(**body, signature_hex=private.sign(DOMAIN + canonical(body)).hex())
    with pytest.raises(GateError, match="SUBJECT_PREDATES_AUTHORITY_ENROLLMENT"):
        r.ledger.publish(r.producer, r.f.plan.plan_id, value)


def test_publication_also_requires_current_plan_read_access(registry):
    r = registry
    r.grants[r.producer.subject_id].remove("gate:read")
    with pytest.raises(GateError, match="AUTHORITY_FORBIDDEN"):
        r.publish("context", r.f.state["context"].fingerprint)


def test_envelope_budget_never_prunes_existing_proof_or_changes_plan(registry, monkeypatch):
    r = registry
    ctx = r.f.state["context"]
    before = r.f.store.snapshot(r.f.plan.plan_id)
    monkeypatch.setattr("bravo_gate.authority_records.MAX_ENVELOPES", 1)
    first = r.publish("context", ctx.fingerprint)
    with pytest.raises(GateError, match="AUTHORITY_RECORD_BUDGET"):
        r.publish("run-source", r.f.state["runs"]["source"].fingerprint)
    assert r.publish("context", ctx.fingerprint) == first
    assert (
        r.view(
            lambda view: r.ledger.authority(view).verify(ctx, ctx.fingerprint, "context")
        ).purpose
        == "context"
    )
    assert r.f.store.snapshot(r.f.plan.plan_id) == before


def test_new_adapter_instance_reads_published_proof_without_process_cache(registry):
    r = registry
    ctx = r.f.state["context"]
    r.publish("context", ctx.fingerprint)
    fresh = PlanAuthorityRecords(r.f.records, clock=lambda: r.clock["now"])
    assert (
        r.view(
            lambda view: fresh.authority(view).verify(ctx, ctx.fingerprint, "context")
        ).subject_digest
        == ctx.fingerprint
    )
    r.ledger.revoke(
        r.manager,
        r.f.plan.plan_id,
        issuer_id="fixture-issuer",
        key_id="context",
        expected_revision=1,
    )
    with pytest.raises(GateError, match="REVOKED_OR_EXPIRED"):
        r.view(lambda view: fresh.authority(view).verify(ctx, ctx.fingerprint, "context"))


CRASHING_PUBLISHER = """
import json, os, sys
from datetime import datetime
from bravo_gate.authority_records import PlanAuthorityRecords
from bravo_gate.signatures import AuthorityEnvelope
from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from pathlib import Path
data=json.loads(Path(sys.argv[1]).read_bytes())
store=(PostgresPlanStore(data['store']) if data['backend']=='postgres' else PlanStore(data['store']))
p=Principal(data['subject'],data['tenant'])
records=GateRecords(store,authorize=lambda c,who,row,action:who==p
    and row['plan_id']==data['plan'] and action in {'gate:read','gate:publish-provenance'})
ledger=PlanAuthorityRecords(records,clock=lambda:datetime.fromisoformat(data['now']))
if data['stage']=='before-commit':
    original=ledger._insert
    def terminate(*args):
        original(*args)
        os._exit(91)
    ledger._insert=terminate
ledger.publish(p,data['plan'],AuthorityEnvelope(**data['envelope']))
os._exit(92)
"""


@pytest.mark.parametrize("stage", ["before-commit", "after-commit-before-response"])
def test_process_death_preserves_atomic_publication_and_identical_retry(registry, tmp_path, stage):
    """Kill the publisher without finally/connection cleanup, then use a new connection."""
    r = registry
    ctx = r.f.state["context"]
    before = r.f.store.snapshot(r.f.plan.plan_id)
    envelope = r.envelope("context", ctx.fingerprint)
    payload = {
        "backend": "postgres" if hasattr(r.f.store, "_conninfo") else "sqlite",
        "store": r.f.store._conninfo if hasattr(r.f.store, "_conninfo") else str(r.f.store.path),
        "plan": r.f.plan.plan_id,
        "tenant": r.producer.tenant_id,
        "subject": r.producer.subject_id,
        "now": r.clock["now"].isoformat(),
        "envelope": asdict(envelope),
        "stage": stage,
    }
    # Synthetic DB credentials stay in a private fixture file, never argv/stdout.
    fixture = tmp_path / "crash-fixture.json"
    with open(fixture, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(payload, output)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    child = subprocess.run(
        [sys.executable, "-c", CRASHING_PUBLISHER, str(fixture)],
        capture_output=True,
        timeout=20,
        env=env,
    )
    # Keep child diagnostics out of a public assertion on any unexpected failure.
    assert child.returncode == (91 if stage == "before-commit" else 92), (
        "publisher_crash_unconfirmed"
    )
    prior = r.view(
        lambda view: r.ledger.authority(view).envelope_provider(ctx, ctx.fingerprint, "context")
    )
    assert (prior is None) == (stage == "before-commit")
    first = r.ledger.publish(r.producer, r.f.plan.plan_id, envelope)
    assert r.ledger.publish(r.producer, r.f.plan.plan_id, envelope) == first
    assert (
        r.view(
            lambda view: r.ledger.authority(view).verify(ctx, ctx.fingerprint, "context")
        ).subject_digest
        == ctx.fingerprint
    )
    with r.f.store._transaction(write=False, plan_id=r.f.plan.plan_id) as c:
        assert (
            c.execute(
                "SELECT count(*) AS n FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                (r.f.plan.plan_id, "t19-authority-envelope:%"),
            ).fetchone()["n"]
            == 1
        )
    assert r.f.store.snapshot(r.f.plan.plan_id) == before
