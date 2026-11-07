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
        gate_signer.sign(f.call, keys, f.plan.plan_id, f.clock.at, records.__setitem__)
    assert f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]["published"] == []


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
