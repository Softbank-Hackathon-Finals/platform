"""Synthetic authentication fixtures: never evidence of operational NET PASS."""

from dataclasses import replace
from datetime import timedelta

import pytest

from bravo_gate.contract import GateError, canonical, fingerprint
from bravo_gate.models import VerifiedAuthority
from bravo_gate.network import VerifiedNetworkProof
from bravo_gate.network_bundle import (
    CollectedNetworkProofAuthority,
    NetworkPathRegistration,
    NetworkRunRegistration,
    output_digest,
    verify_collected_bundle,
)


def make(bundle, method="1", test_id="NET-01.3"):
    ctx, _, target, now = bundle
    stamp = now.isoformat()
    paths = tuple(
        NetworkPathRegistration(
            role,
            port,
            "sha256:" + key * 64,
            "sha256:" + "c" * 64,
            None if method == "1" else "sha256:" + "d" * 64,
        )
        for role, port, key in (
            ("database", 5432, "a"),
            ("pooler", 6432 if method == "1" else 5432, "b"),
        )
    )
    registration = NetworkRunRegistration(
        ctx.fingerprint,
        "target",
        ctx.app,
        ctx.target_environment_ref,
        target["run_id"],
        test_id,
        method,
        paths,
        (now + timedelta(seconds=40)).isoformat(),
    )
    envelope = dict(
        format_version=method,
        run_id=target["run_id"],
        app=ctx.app,
        environment=ctx.target_environment_ref,
        test_id=test_id,
        observed="deny",
        unknown_reason=None,
        collected_at=stamp,
    )
    docs = []
    for path in paths:
        observations = {}
        for label in ("before_target", "before_agent", "probe", "after_target", "after_agent") + (
            ("before_dns", "after_dns") if test_id == "NET-01.3" else ()
        ):
            observations[label] = dict(
                outcome="refused"
                if label == "probe"
                else "resolved"
                if label.endswith("dns")
                else "connected",
                elapsed_ms=2,
                observed_at=stamp,
            )
            if method == "2":
                observations[label].update(
                    coordinator_started_at=stamp, agent_observed_at=stamp, max_clock_offset_ms=1000
                )
        docs.append(
            dict(
                envelope,
                target_binding_sha256=path.target_binding_sha256,
                policy_sha256=path.policy_sha256,
                observations=observations,
                positive_control="pass",
                qualification="bounded-connectivity-with-controls-not-policy-causality",
            )
        )
    aggregate = dict(envelope, results=[])
    if method == "1":
        aggregate["required_ports"] = [5432, 6432]
    else:
        aggregate.update(
            method_version="2",
            required_roles=["database", "pooler"],
            registration_binding_sha256="sha256:"
            + fingerprint(
                [
                    dict(
                        role=p.role,
                        target_binding_sha256=p.target_binding_sha256,
                        registration_sha256=p.registration_sha256,
                    )
                    for p in paths
                ]
            ),
        )
    for path in paths:
        row = dict(
            port=path.port,
            target_binding_sha256=path.target_binding_sha256,
            output_sha256="",
            observed="deny",
            positive_control="pass",
            unknown_reason=None,
        )
        if method == "2":
            row.update(role=path.role, registration_sha256=path.registration_sha256)
        aggregate["results"].append(row)
    return registration, aggregate, docs, now


def encode(aggregate, docs):
    children = {}
    for row, doc in zip(aggregate["results"], docs, strict=False):
        raw = canonical(doc)
        row["output_sha256"] = output_digest(raw)
        children[output_digest(raw)] = raw
    raw = canonical(aggregate)
    return raw, children


def verify(fixture):
    registration, aggregate, docs, now = fixture
    raw, children = encode(aggregate, docs)
    return verify_collected_bundle(
        raw, children, registration=registration, expected_digest=output_digest(raw), now=now
    )


@pytest.mark.parametrize("method", ["1", "2"])
@pytest.mark.parametrize("test_id", ["NET-01.2", "NET-01.3"])
def test_two_registered_paths_with_before_and_after_controls(bundle, method, test_id):
    fixture = make(bundle, method, test_id)
    assert verify(fixture) == fixture[-1] + timedelta(seconds=60)


@pytest.mark.parametrize(
    "case",
    [
        "missing-path",
        "duplicate-path",
        "swapped-role",
        "unknown-result",
        "actual-allow",
        "positive-missing",
        "missing-dns",
        "dns-failed",
        "healthy-target-failed",
        "after-missing",
        "short-timeout",
        "clock-future",
        "clock-old",
        "probe-after-collection",
        "before-after-probe",
        "different-run",
        "different-app",
        "different-environment",
        "policy",
        "target-binding",
        "child-extra",
        "aggregate-extra",
        "wrong-digest",
        "extra-artifact",
        "missing-artifact",
        "expired-registration",
        "unsupported-method",
        "malformed-item",
        "false-port",
    ],
)
@pytest.mark.parametrize("method", ["1", "2"])
def test_one_unverified_path_cannot_produce_complete_network_proof(bundle, case, method):
    registration, aggregate, docs, now = make(bundle, method)
    child = docs[1]
    probe = child["observations"]["probe"]
    if case == "missing-path":
        aggregate["results"].pop()
    if case == "duplicate-path":
        aggregate["results"][1] = aggregate["results"][0].copy()
        docs[1] = docs[0]
    if case == "swapped-role":
        aggregate["results"].reverse()
    if case == "unknown-result":
        aggregate["unknown_reason"] = "TIMEOUT"
    if case == "actual-allow":
        child["observed"] = "allow"
        probe["outcome"] = "connected"
    if case == "positive-missing":
        child["positive_control"] = "not_applicable"
    if case == "missing-dns":
        del child["observations"]["before_dns"]
    if case == "dns-failed":
        child["observations"]["after_dns"]["outcome"] = "error"
    if case == "healthy-target-failed":
        child["observations"]["before_target"]["outcome"] = "refused"
    if case == "after-missing":
        del child["observations"]["after_agent"]
    if case == "short-timeout":
        probe.update(outcome="timeout", elapsed_ms=2)
    if case == "clock-future":
        probe["observed_at"] = (now + timedelta(seconds=1)).isoformat()
    if case == "clock-old":
        probe["observed_at"] = (now - timedelta(seconds=61)).isoformat()
    if case == "probe-after-collection":
        child["collected_at"] = (now - timedelta(seconds=1)).isoformat()
    if case == "before-after-probe":
        probe["observed_at"] = (now - timedelta(seconds=1)).isoformat()
    if case == "different-run":
        child["run_id"] = "other-run"
    if case == "different-app":
        child["app"] = "other-app"
    if case == "different-environment":
        child["environment"] = "other-env"
    if case == "policy":
        child["policy_sha256"] = "sha256:" + "f" * 64
    if case == "target-binding":
        child["target_binding_sha256"] = docs[0]["target_binding_sha256"]
    if case == "child-extra":
        child["not_a_signature"] = True
    if case == "aggregate-extra":
        aggregate["not_a_signature"] = True
    if case == "expired-registration":
        registration = replace(registration, valid_until=now.isoformat())
    if case == "unsupported-method":
        aggregate["format_version"] = "3"
    if case == "malformed-item":
        child["observations"]["probe"] = None
    if case == "false-port":
        aggregate["results"][1]["port"] = True
    raw, children = encode(aggregate, docs)
    if case == "wrong-digest":
        raw += b" "
    if case == "extra-artifact":
        children["sha256:" + "e" * 64] = b"{}"
    if case == "missing-artifact":
        children.pop(next(iter(children)))
    with pytest.raises(GateError):
        verify_collected_bundle(
            raw,
            children,
            registration=registration,
            expected_digest=output_digest(canonical(aggregate)),
            now=now,
        )


@pytest.mark.parametrize("case", ["offset", "budget", "interval", "allowance", "registry", "role"])
def test_method_two_clock_and_registry_contract_cannot_be_relaxed(bundle, case):
    fixture = make(bundle, "2")
    _, aggregate, docs, now = fixture
    item = docs[1]["observations"]["probe"]
    if case == "offset":
        item["agent_observed_at"] = (now + timedelta(seconds=2)).isoformat()
    if case == "budget":
        item["coordinator_started_at"] = (now - timedelta(seconds=6)).isoformat()
    if case == "interval":
        item["coordinator_started_at"] = (now + timedelta(seconds=1)).isoformat()
    if case == "allowance":
        item["max_clock_offset_ms"] = 9999
    if case == "registry":
        aggregate["registration_binding_sha256"] = "sha256:" + "f" * 64
    if case == "role":
        aggregate["results"][1]["role"] = "database"
    with pytest.raises(GateError):
        verify(fixture)


def authority(bundle, method="1"):
    registration, aggregate, docs, now = make(bundle, method)
    raw, children = encode(aggregate, docs)
    key = output_digest(raw)
    artifacts = {key: raw, **children}
    state = dict(
        binding=registration,
        artifacts=artifacts,
        trusted=True,
        reads=0,
        expiry=now + timedelta(seconds=10),
        change=lambda *_: None,
    )

    def fetch(*args):
        state["reads"] += 1
        state["change"](state["reads"])
        return state["artifacts"][args[-1]]

    def authenticate_binding(*_):
        return (
            VerifiedAuthority(
                bundle[0].fingerprint, registration.fingerprint, "network-binding", state["expiry"]
            )
            if state["trusted"]
            else False
        )

    def authenticate_artifact(*args):
        return (
            VerifiedAuthority(
                bundle[0].fingerprint, args[-2][7:], "network-artifact", state["expiry"]
            )
            if state["trusted"]
            else False
        )

    verifier = CollectedNetworkProofAuthority(
        binding_provider=lambda *_: state["binding"],
        authenticate_binding=authenticate_binding,
        artifact_provider=fetch,
        authenticate_artifact=authenticate_artifact,
        clock=lambda: now,
    )
    row = dict(
        method_version=method,
        evidence=dict(output_sha256=key, collected_at=aggregate["collected_at"]),
    )
    return verifier, state, row


@pytest.mark.parametrize("method", ["1", "2"])
def test_authority_reauthenticates_three_artifacts_and_preserves_expiry(bundle, method):
    verifier, state, row = authority(bundle, method)
    proof = verifier("actor", bundle[0], "target", "NET-01.3", row)
    assert proof.required_roles_verified == ("database", "pooler")
    assert proof.run_id == bundle[2]["run_id"]
    assert proof.valid_until == state["expiry"]
    assert state["reads"] == 6


@pytest.mark.parametrize("point", range(1, 7))
def test_revocation_at_every_original_and_readback_lookup_denies(bundle, point):
    verifier, state, row = authority(bundle)

    def change(index):
        if index == point:
            state["trusted"] = False

    state["change"] = change
    with pytest.raises(GateError):
        verifier("actor", bundle[0], "target", "NET-01.3", row)


@pytest.mark.parametrize(
    "case",
    [
        "changed-binding",
        "changed-artifact",
        "different-context",
        "different-method",
        "different-time",
    ],
)
def test_no_replay_or_mutation_between_collection_and_gate(bundle, case):
    verifier, state, row = authority(bundle)
    ctx = bundle[0]
    if case == "changed-binding":
        state["binding"] = replace(state["binding"], app="other")
    if case == "changed-artifact":
        state["artifacts"][row["evidence"]["output_sha256"]] += b" "
    if case == "different-context":
        ctx = replace(ctx, plan_digest="f" * 64)
    if case == "different-method":
        row["method_version"] = "2"
    if case == "different-time":
        row["evidence"]["collected_at"] = (bundle[-1] - timedelta(seconds=1)).isoformat()
    with pytest.raises(GateError):
        verifier("actor", ctx, "target", "NET-01.3", row)


def test_gate_rejects_legacy_single_path_and_wrong_run_even_if_same_digest(service, bundle):
    app, state = service
    ctx, _, target, now = bundle
    verifier, _, row = authority(bundle)
    app.network_verifier = verifier
    runs = [state["runs"][side] for side in ("source", "target")]
    check = app._network("actor", ctx, [], runs)
    assert check("target", "NET-01.3", row)
    proof = verifier("actor", ctx, "target", "NET-01.3", row)
    for invalid in (
        replace(proof, required_roles_verified=()),
        replace(proof, run_id="other-run"),
        VerifiedNetworkProof(
            ctx.fingerprint,
            "target",
            "NET-01.3",
            row["evidence"]["output_sha256"],
            now + timedelta(seconds=10),
        ),
    ):
        app.network_verifier = lambda *_, value=invalid: value
        assert not check("target", "NET-01.3", row)


@pytest.mark.parametrize("point", [4, 5, 6])
def test_artifact_replacement_during_readback_is_not_accepted(bundle, point):
    verifier, state, row = authority(bundle)

    def change(index):
        if index == point:
            for key in state["artifacts"]:
                state["artifacts"][key] += b" "

    state["change"] = change
    with pytest.raises(GateError):
        verifier("actor", bundle[0], "target", "NET-01.3", row)


def test_binding_replaced_after_original_reads_is_not_accepted(bundle):
    verifier, state, row = authority(bundle)

    def change(index):
        if index == 3:
            state["binding"] = replace(state["binding"], run_id="replacement-run")

    state["change"] = change
    with pytest.raises(GateError):
        verifier("actor", bundle[0], "target", "NET-01.3", row)


def test_authority_expiry_during_last_readback_is_not_discarded(bundle):
    verifier, state, row = authority(bundle)

    def change(index):
        if index == 6:
            state["expiry"] = bundle[-1]

    state["change"] = change
    with pytest.raises(GateError):
        verifier("actor", bundle[0], "target", "NET-01.3", row)
