from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bravo_gate.contract import GateError, canonical
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope, AuthorityKey, Ed25519Authority


@pytest.fixture
def authority(bundle):
    ctx, _, _, now = bundle
    key = Ed25519PrivateKey.generate()
    trust = AuthorityKey(
        key.public_key().public_bytes_raw().hex(),
        (ctx.tenant_id,),
        ("exception",),
        (now - timedelta(minutes=1)).isoformat(),
        (now + timedelta(minutes=1)).isoformat(),
        False,
    )
    envelope = AuthorityEnvelope(
        "t10-approval-store",
        "key-one",
        "exception",
        ctx.fingerprint,
        "c" * 64,
        (now - timedelta(seconds=1)).isoformat(),
        (now + timedelta(seconds=30)).isoformat(),
        "0" * 128,
    )
    envelope = replace(envelope, signature_hex=key.sign(DOMAIN + canonical(envelope.payload)).hex())
    state = {"key": trust, "envelope": envelope, "now": now}
    verifier = Ed25519Authority(
        envelope_provider=lambda *_: state["envelope"],
        key_provider=lambda *_: state["key"],
        clock=lambda: state["now"],
    )
    return verifier, state, ctx


def test_real_ed25519_provenance_is_scope_and_purpose_bound(authority):
    verifier, _, ctx = authority
    verifier.verify(ctx, "c" * 64, "exception")
    for subject, purpose, context in [
        ("d" * 64, "exception", ctx),
        ("c" * 64, "run-source", ctx),
        ("c" * 64, "exception", replace(ctx, tenant_id="other")),
    ]:
        with pytest.raises(GateError):
            verifier.verify(context, subject, purpose)


@pytest.mark.parametrize(
    "case", ["revoked", "expired", "tampered", "wrong-role", "future", "other-key"]
)
def test_key_revocation_time_and_tampering_reject(authority, case):
    verifier, state, ctx = authority
    if case == "revoked":
        state["key"] = replace(state["key"], revoked=True)
    if case == "expired":
        state["now"] += timedelta(seconds=31)
    if case == "tampered":
        state["envelope"] = replace(state["envelope"], signature_hex="0" * 128)
    if case == "wrong-role":
        state["key"] = replace(state["key"], purposes=("run-source",))
    if case == "future":
        state["now"] -= timedelta(seconds=2)
    if case == "other-key":
        state["key"] = replace(state["key"], public_key_hex="a" * 64)
    with pytest.raises(GateError):
        verifier.verify(ctx, "c" * 64, "exception")


def test_revocation_during_verification_cannot_return_accepted(authority):
    verifier, state, ctx = authority
    calls = 0

    def current(*_):
        nonlocal calls
        calls += 1
        return state["key"] if calls == 1 else replace(state["key"], revoked=True)

    verifier.key_provider = current
    with pytest.raises(GateError):
        verifier.verify(ctx, "c" * 64, "exception")


def test_key_scope_is_explicit_tuple_not_substring(authority):
    _, state, _ = authority
    with pytest.raises(GateError):
        replace(state["key"], tenants="tenant-one")


def test_real_signatures_are_composed_into_service_consumption(service):
    app, state = service
    ctx = state["context"]
    now = state["now"]
    envelopes = {}
    keys = {}
    for purpose, subject in [
        ("context", ctx.fingerprint),
        ("run-source", ctx.source_digest),
        ("run-target", ctx.target_digest),
    ]:
        key = Ed25519PrivateKey.generate()
        keys[purpose] = AuthorityKey(
            key.public_key().public_bytes_raw().hex(),
            (ctx.tenant_id,),
            (purpose,),
            (now - timedelta(minutes=1)).isoformat(),
            (now + timedelta(minutes=1)).isoformat(),
            False,
        )
        envelope = AuthorityEnvelope(
            purpose,
            "registered-key",
            purpose,
            ctx.fingerprint,
            subject,
            (now - timedelta(seconds=1)).isoformat(),
            (now + timedelta(seconds=30)).isoformat(),
            "0" * 128,
        )
        envelopes[purpose] = replace(
            envelope, signature_hex=key.sign(DOMAIN + canonical(envelope.payload)).hex()
        )
    verifier = Ed25519Authority(
        envelope_provider=lambda _, __, purpose: envelopes[purpose],
        key_provider=lambda issuer, _: keys[issuer],
        clock=lambda: state["now"],
    )
    app.authenticate_context = lambda _, context: verifier.verify(
        context, context.fingerprint, "context"
    )
    app.authenticate_run = lambda _, context, side, snapshot: verifier.verify(
        context, snapshot.fingerprint, "run-" + side
    )
    report = app.evaluate("actor", "context")
    assert not app.require_security("actor", "context", canonical(report)).authorizes_cutover
    keys["run-target"] = replace(keys["run-target"], revoked=True)
    with pytest.raises(GateError):
        app.require_security("actor", "context", canonical(report))
