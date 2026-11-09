from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from bravo_gate.contract import GateError, canonical, time
from bravo_gate.models import VerifiedAuthority, permission_subject


@pytest.mark.parametrize("change", ["context", "subject", "purpose", "expired"])
def test_authenticated_receipt_cannot_change_scope_or_expire(service, change):
    app, state = service
    ctx = state["context"]
    proof = VerifiedAuthority(
        ctx.fingerprint, ctx.fingerprint, "context", state["now"] + timedelta(seconds=1)
    )
    if change == "context":
        proof = replace(proof, context_digest="b" * 64)
    elif change == "subject":
        proof = replace(proof, subject_digest="b" * 64)
    elif change == "purpose":
        proof = replace(proof, purpose="run-source")
    else:
        proof = replace(proof, valid_until=state["now"])
    app.authenticate_context = lambda *_: proof
    with pytest.raises(GateError, match="AUTHORITY_BINDING_OR_EXPIRY"):
        app.evaluate("actor", "context")


def test_permission_expiry_caps_prerequisite(service):
    app, state = service
    expiry = state["now"] + timedelta(seconds=1)
    app.authorize = lambda actor, ctx, action: VerifiedAuthority(
        ctx.fingerprint,
        permission_subject(actor, ctx.fingerprint, action),
        "permission:" + action,
        expiry,
    )
    report = app.evaluate("actor", "context")
    grant = app.require_security("actor", "context", canonical(report))
    assert time(grant.valid_until) == expiry


def test_other_actor_permission_receipt_cannot_be_replayed(service):
    app, state = service
    ctx = state["context"]
    app.authorize = lambda _actor, _ctx, action: VerifiedAuthority(
        ctx.fingerprint,
        permission_subject("other-actor", ctx.fingerprint, action),
        "permission:" + action,
        state["now"] + timedelta(seconds=5),
    )
    with pytest.raises(GateError, match="AUTHORITY_BINDING_OR_EXPIRY"):
        app.evaluate("actor", "context")


@pytest.mark.parametrize(
    "boundary", ["context_provider", "run_provider", "authenticate_run", "authorize"]
)
def test_backend_failure_does_not_leak_error_or_reuse_prior_pass(service, boundary):
    app, _ = service
    report = app.evaluate("actor", "context")

    def fail(*_):
        raise RuntimeError("simulated-private-provider-error")

    setattr(app, boundary, fail)
    with pytest.raises(GateError, match="^AUTHORITY_UNAVAILABLE$") as caught:
        app.require_security("actor", "context", canonical(report))
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("clock_value", [None, "2026-11-04", datetime(2026, 11, 4)])
def test_invalid_server_clock_cannot_issue_result(service, clock_value):
    app, state = service
    state["now"] = clock_value
    with pytest.raises(GateError, match="INVALID_SERVER_CLOCK"):
        app.evaluate("actor", "context")


def test_authority_expires_during_last_readback(service):
    app, state = service
    expiry = state["now"] + timedelta(seconds=1)
    calls = 0

    def authenticate(_actor, ctx):
        nonlocal calls
        calls += 1
        if calls == 3:
            state["now"] = expiry
        return VerifiedAuthority(ctx.fingerprint, ctx.fingerprint, "context", expiry)

    app.authenticate_context = authenticate
    with pytest.raises(GateError, match="AUTHORITY_BINDING_OR_EXPIRY"):
        app.evaluate("actor", "context")


def test_consume_permission_revoked_during_final_readback(service):
    app, _ = service
    report = app.evaluate("actor", "context")
    calls = 0

    def authorize(_actor, _ctx, action):
        nonlocal calls
        if action == "gate:consume":
            calls += 1
            return calls < 3
        return True

    app.authorize = authorize
    with pytest.raises(GateError, match="AUTHORITY_DENIED"):
        app.require_security("actor", "context", canonical(report))
