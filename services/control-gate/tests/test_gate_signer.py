"""T19 platform signer (ADR-0038) on a real T10 store: KMS-held keys through the operator's relay path."""

import json
from datetime import timedelta

import pytest
from bravo_platform_events import gate_signer
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from test_gate_assembly import gate  # noqa: F401
from test_gate_relay import bind_through_the_relay, relay, run_doc  # noqa: F401
from test_readiness import ready  # noqa: F401

from bravo_gate.contract import GateError

ARN = "arn:aws:kms:ap-northeast-2:111122223333:key/{:08d}-0000-4000-8000-000000000000"


class FakeKms:
    """GetPublicKey/Sign as KMS answers them for an ECC_NIST_EDWARDS25519 key; the private key stays here."""

    def __init__(self, spec="ECC_NIST_EDWARDS25519"):
        self.spec, self.keys, self.signed, self.forge = spec, {}, [], None

    def get_public_key(self, KeyId):  # noqa: N803 - boto3 parameter names
        key = self.keys.setdefault(KeyId, Ed25519PrivateKey.generate())
        return {"KeySpec": self.spec, "KeyUsage": "SIGN_VERIFY",
                "SigningAlgorithms": ["ED25519_SHA_512", "ED25519_PH_SHA_512"],
                "PublicKey": key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)}

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):  # noqa: N803
        assert (MessageType, SigningAlgorithm) == ("RAW", "ED25519_SHA_512")
        self.signed.append(KeyId)
        return {"Signature": (self.forge or self.keys[KeyId]).sign(Message)}


def platform_keys(kms):
    return {p: gate_signer.KmsKey(ARN.format(i), kms) for i, p in enumerate(gate_signer.PURPOSES)}


def test_platform_keys_enroll_sign_and_the_same_verifier_passes_the_gate(relay):  # noqa: F811
    f, kms, records = relay, FakeKms(), {}
    keys = platform_keys(kms)
    assert gate_signer.enroll(f.call, keys, f.plan.plan_id, f.clock.at) == (list(gate_signer.PURPOSES), [])
    assert gate_signer.enroll(f.call, keys, f.plan.plan_id, f.clock.at)[0]  # a retry in the hour: identical
    f.clock.at = f.state["now"]
    f.base = f.state["context"]
    bind_through_the_relay(f, run_doc(f, "source"), run_doc(f, "target"))

    def put(plan_id, key, value):
        records.setdefault(key, value)

    signed = gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put)
    assert set(signed) == set(gate_signer.PURPOSES) - {"gate-result"}  # nothing evaluated yet
    assert f.call({"op": "evaluate", "plan_id": f.plan.plan_id})["evaluated"]["decision"] == "PASS"
    assert gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put) == ["gate-result"]
    assert gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put) == []  # no conflict
    assert {v["purpose"] for v in records.values()} == set(gate_signer.PURPOSES)
    assert all("delegation_id" not in v for v in records.values())  # no unchecked delegation id (ADR-0038)
    assert all(v["issuer_id"] == "platform-" + v["purpose"] and v["kms_key_arn"] == keys[v["purpose"]].arn
               for v in records.values())
    assert "PRIVATE" not in json.dumps(records) and len(set(kms.signed)) == len(gate_signer.PURPOSES)


def test_enrollment_from_an_earlier_hour_is_reused_not_a_failure(relay):  # noqa: F811
    f, keys = relay, platform_keys(FakeKms())
    gate_signer.enroll(f.call, keys, f.plan.plan_id, f.clock.at)
    assert gate_signer.enroll(f.call, keys, f.plan.plan_id, f.clock.at + timedelta(hours=2)) == ([], list(keys))


def test_a_wrong_kms_answer_is_refused_before_anything_is_published(relay):  # noqa: F811
    f, kms = relay, FakeKms()
    keys = platform_keys(kms)
    gate_signer.enroll(f.call, keys, f.plan.plan_id, f.clock.at)
    f.clock.at = f.state["now"]
    f.base = f.state["context"]
    bind_through_the_relay(f, run_doc(f, "source"), run_doc(f, "target"))
    kms.forge = Ed25519PrivateKey.generate()
    records = {}
    with pytest.raises(InvalidSignature):
        gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, lambda _plan, key, value: records.update({key: value}))
    assert f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]["published"] == []
    assert [v.get("signature_sha256") for v in records.values()] == [None]  # the intent only


def test_signing_needs_a_kms_ed25519_key():
    with pytest.raises(GateError, match="PLATFORM_SIGNER_KEY_INVALID"):
        gate_signer.KmsKey(ARN.format(0), FakeKms(spec="ECC_NIST_P256"))


def test_config_names_one_distinct_kms_key_per_purpose(tmp_path):
    relay_config = {"tenant_id": "t", "admin": "a", "issuer": "i", "operator": "o"}
    keys = {p: ARN.format(i) for i, p in enumerate(gate_signer.PURPOSES)}
    path = tmp_path / "gate-signer.json"
    path.write_text(json.dumps({"relay": relay_config, "keys": keys}))
    _, loaded = gate_signer.load(path, client=FakeKms())
    assert set(loaded) == set(gate_signer.PURPOSES)
    for bad in ({**keys, "context": keys["run-source"]},
                {**keys, "context": "arn:aws:kms:ap-northeast-2:111122223333:alias/gate"},
                {k: v for k, v in keys.items() if k != "gate-result"}):
        path.write_text(json.dumps({"relay": relay_config, "keys": bad}))
        with pytest.raises(ValueError):
            gate_signer.load(path, client=FakeKms())


# ADR-0038 GATE stage: the platform signs only under the plan's live `evidence` delegation (#716).
def bound_with_platform_keys(f, kms):
    keys = platform_keys(kms)
    gate_signer.enroll(f.call, keys, f.plan.plan_id, f.clock.at)
    f.clock.at = f.state["now"]
    f.base = f.state["context"]
    bind_through_the_relay(f, run_doc(f, "source"), run_doc(f, "target"))
    return keys


def delegations(f):
    from bravo_platform_events.approval_delegation import ApprovalDelegations

    return ApprovalDelegations(f.store, f.access, clock=lambda: f.clock.at.isoformat())


def version(f):
    with f.store._transaction(plan_id=f.plan.plan_id) as c:
        return c.execute("SELECT version FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone()["version"]


def issuer(f):
    """The issuer as the relay authenticates it now (the fixture's own principal predates its grant)."""
    from bravo_platform_events.gate_relay import _principal

    return _principal("gate-issuer", f.plan.tenant_id, lambda: f.clock.at)


def delegate(f, d, *, minutes=60, gates=("evidence",)):
    return d.delegate(issuer(f), f.plan.plan_id, gates=list(gates), bounds={},
                      expires_at=(f.clock.at + timedelta(minutes=minutes)).isoformat(),
                      command_key="dlg-evidence-" + str(minutes))


def test_a_live_evidence_delegation_signs_and_names_itself_in_every_record(relay):  # noqa: F811
    from bravo_platform_events.store import EventError

    f, kms, records = relay, FakeKms(), {}
    keys = bound_with_platform_keys(f, kms)
    d = delegations(f)
    granted = delegate(f, d)
    check = gate_signer.delegated(d, f.plan.plan_id, version(f))
    signed = gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, lambda _plan, key, value: records.setdefault(key, value), delegation=check)
    assert set(signed) == set(gate_signer.PURPOSES) - {"gate-result"}
    assert {v["delegation_id"] for v in records.values()} == {granted["delegation_id"]}
    assert f.call({"op": "evaluate", "plan_id": f.plan.plan_id})["evaluated"]["decision"] == "PASS"
    with pytest.raises(EventError, match="delegation_unavailable"):
        check("0" * 64)  # one decision per version: another context in the same version is refused


def regrant(f, permissions, *, valid_after):
    f.access.set_permissions(f.plan.plan_id, issuer(f), permissions=permissions, valid_after=valid_after.isoformat(),
                             expires_at=(f.clock.at + timedelta(hours=1)).isoformat(), expected_revision=1,
                             administration_ref="test-admin", command_key="regrant-issuer")


@pytest.mark.parametrize("case", ["none", "expired", "revoked", "other_gate", "narrowed", "granted_after"])
def test_without_a_live_evidence_delegation_nothing_is_recorded_or_signed(relay, case):  # noqa: F811
    from bravo_platform_events.store import EventError

    f, kms, records = relay, FakeKms(), {}
    keys = bound_with_platform_keys(f, kms)
    d = delegations(f)
    if case == "expired":
        delegate(f, d, minutes=1)
        f.clock.at += timedelta(minutes=2)
    elif case == "revoked":
        delegate(f, d)
        d.revoke(issuer(f), f.plan.plan_id)
    elif case == "other_gate":
        f.access.set_permissions(f.plan.plan_id, issuer(f), permissions=["read", "gate:read", "gate:publish-provenance",
                                                                        "approve_change"],
                                 valid_after=(f.clock.at - timedelta(hours=1)).isoformat(),
                                 expires_at=(f.clock.at + timedelta(hours=1)).isoformat(), expected_revision=1,
                                 administration_ref="test-admin", command_key="grant-issuer-change")
        delegate(f, d, gates=("change",))  # an approval delegation does not cover the T19 evidence
    elif case == "narrowed":  # #828 review: the delegator's grant now, not at delegation time
        delegate(f, d)
        regrant(f, ["read", "gate:read"], valid_after=f.clock.at - timedelta(hours=1))
    elif case == "granted_after":  # a grant newer than the delegation does not count
        delegate(f, d)
        f.clock.at += timedelta(minutes=1)
        regrant(f, ["read", "gate:read", "gate:publish-provenance"], valid_after=f.clock.at)
    with pytest.raises(EventError, match="delegation_unavailable"):
        gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, lambda _plan, key, value: records.setdefault(key, value),
                         delegation=gate_signer.delegated(d, f.plan.plan_id, version(f)))
    assert records == {} and kms.signed == []
    assert f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]["published"] == []


def test_nothing_left_needs_no_delegation_and_the_platform_never_signs_its_gate_result(relay):  # noqa: F811
    f, kms, records = relay, FakeKms(), {}
    keys = bound_with_platform_keys(f, kms)
    d = delegations(f)
    delegate(f, d)
    check = gate_signer.delegated(d, f.plan.plan_id)  # version read where the use is recorded

    def put(_plan, key, value):
        records.setdefault(key, value)

    assert set(gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put, delegation=check)) == (
        set(gate_signer.PURPOSES) - {"gate-result"})
    assert f.call({"op": "evaluate", "plan_id": f.plan.plan_id})["evaluated"]["decision"] == "PASS"
    made = len(kms.signed)
    # the second sweep: the evaluation's gate-result is not measurement provenance (#565)
    assert gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put, delegation=check) == []
    d.revoke(issuer(f), f.plan.plan_id)
    # an operator-signed or finished plan holds no GATE for a delegation it no longer needs
    assert gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put, delegation=check) == []
    assert len(kms.signed) == made and "gate-result" not in {v["purpose"] for v in records.values()}


def test_the_intent_is_recorded_before_kms_signs_and_each_signature_once(relay):  # noqa: F811
    f, kms, records = relay, FakeKms(), {}
    keys = bound_with_platform_keys(f, kms)

    def put(_plan, key, value):
        records.setdefault(key, value)

    def down(**_):
        raise RuntimeError("kms unavailable")

    kms.sign = down
    with pytest.raises(Exception):  # noqa: B017 - whatever the client raises
        gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put)
    assert [v.get("signature_sha256") for v in records.values()] == [None]  # the intent, no signature
    del kms.sign
    signed = gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, put)
    made = [v for v in records.values() if "signature_sha256" in v]
    assert len(made) == len(kms.signed) == len(signed)  # one record per kms:Sign

