import hashlib
from datetime import timedelta

import pytest

from bravo_gate.contract import GateError, canonical
from bravo_gate.models import VerifiedAuthority
from bravo_gate.network import NetworkBinding, NetworkProofAuthority, verify_network_proof


@pytest.fixture
def network(bundle):
    ctx, _, _, now = bundle
    at = now.isoformat()
    binding = NetworkBinding(
        ctx.fingerprint,
        ctx.target_environment_ref,
        "db:5432",
        "untrusted-pod",
        "allowed-app",
        "allowed-service:443",
        "cluster-dns",
        "a" * 64,
        3000,
        (now + timedelta(minutes=1)).isoformat(),
    )
    doc = {
        "kind": "network-denial-proof.v1",
        "test_id": "NET-01.3",
        "environment_ref": ctx.target_environment_ref,
        "target_ref": "db:5432",
        "probe_ref": "untrusted-pod",
        "configuration_digest": "a" * 64,
        "probe": {
            "result": "timeout",
            "at": at,
            "duration_ms": 3000,
            "target_ref": "db:5432",
            "probe_ref": "untrusted-pod",
        },
        "controls": [
            {
                "kind": "target",
                "result": "success",
                "at": at,
                "target_ref": "db:5432",
                "probe_ref": "allowed-app",
            },
            {
                "kind": "probe-dns",
                "result": "success",
                "at": at,
                "target_ref": "cluster-dns",
                "probe_ref": "untrusted-pod",
            },
            {
                "kind": "probe-path",
                "result": "success",
                "at": at,
                "target_ref": "allowed-service:443",
                "probe_ref": "untrusted-pod",
            },
        ],
    }
    return ctx, now, binding, doc


def parse(binding, doc, now):
    raw = canonical(doc)
    return verify_network_proof(
        raw,
        output_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        binding=binding,
        test_id="NET-01.3",
        now=now,
    )


def test_timeout_requires_same_target_source_window_and_allowed_controls(network):
    _, now, binding, doc = network
    assert parse(binding, doc, now)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "target",
        "probe",
        "wrong-control-origin",
        "wrong-allowed-service",
        "dns",
        "failed",
        "old",
        "config",
        "short-timeout",
        "future",
    ],
)
def test_ambiguous_timeout_never_establishes_denial(network, case):
    _, now, binding, doc = network
    if case == "missing":
        doc["controls"].pop()
    if case == "target":
        doc["probe"]["target_ref"] = "other-db"
    if case == "probe":
        doc["probe"]["probe_ref"] = "other-probe"
    if case == "wrong-control-origin":
        doc["controls"][0]["probe_ref"] = "any-probe"
    if case == "wrong-allowed-service":
        doc["controls"][2]["target_ref"] = "unregistered-service"
    if case == "dns":
        doc["controls"][1]["target_ref"] = "unregistered-dns"
    if case == "failed":
        doc["controls"][0]["result"] = "failed"
    if case == "old":
        doc["controls"][0]["at"] = (now - timedelta(seconds=61)).isoformat()
    if case == "config":
        doc["configuration_digest"] = "b" * 64
    if case == "short-timeout":
        doc["probe"]["duration_ms"] = 1
    if case == "future":
        doc["probe"]["at"] = (now + timedelta(seconds=1)).isoformat()
    assert not parse(binding, doc, now)


def test_raw_output_bytes_and_authenticated_binding_are_required(network):
    ctx, now, binding, doc = network
    raw = canonical(doc)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    assert not verify_network_proof(
        raw + b" ", output_digest=digest, binding=binding, test_id="NET-01.3", now=now
    )
    state = {"trusted": True, "binding": binding}
    authority = NetworkProofAuthority(
        binding_provider=lambda *_: state["binding"],
        authenticate_binding=lambda *_: state["trusted"],
        artifact_provider=lambda *_: raw,
        authenticate_artifact=lambda *_: state["trusted"],
        clock=lambda: now,
    )
    row = {"evidence": {"output_sha256": digest}}
    assert authority("actor", ctx, "target", "NET-01.3", row).context_digest == ctx.fingerprint
    state["trusted"] = False
    with pytest.raises(GateError):
        authority("actor", ctx, "target", "NET-01.3", row)


def test_network_authentication_expiry_is_not_discarded(network):
    ctx, now, binding, doc = network
    raw = canonical(doc)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    expiry = now + timedelta(seconds=1)
    authority = NetworkProofAuthority(
        binding_provider=lambda *_: binding,
        authenticate_binding=lambda *_: VerifiedAuthority(
            ctx.fingerprint, binding.fingerprint, "network-binding", expiry
        ),
        artifact_provider=lambda *_: raw,
        authenticate_artifact=lambda *_: VerifiedAuthority(
            ctx.fingerprint, digest.removeprefix("sha256:"), "network-artifact", expiry
        ),
        clock=lambda: now,
    )
    proof = authority("actor", ctx, "target", "NET-01.3", {"evidence": {"output_sha256": digest}})
    assert proof.valid_until == expiry
