"""T19 platform signer (ADR-0038): the gate's envelopes signed by AWS KMS Ed25519 keys the platform holds.

One non-exportable KMS key per purpose: T19 accepts a public key for one purpose only, once per plan.
Any workflow-host replica signs. boto3 exchanges the pod's projected ServiceAccount token
(AWS_WEB_IDENTITY_TOKEN_FILE, audience sts.amazonaws.com) for the signer role (AWS_ROLE_ARN). That role
may only call kms:Sign with ED25519_SHA_512 on these keys (infra/envs/platform-gate-signer).

The keys are enrolled per plan through the same relay operations as an operator key, under issuer ids
`platform-<purpose>`. The T19 verifier is unchanged and accepts whichever registered key signed;
operations/gate/run.py stays valid. The relay subjects come from this signer's own config, so its
grants and records are separate from the operator's.

    /workflow/gate-signer.json (ConfigMap, names only, no secret values):
    {"relay": {"tenant_id", "admin", "issuer", "operator"},
     "keys": {"<purpose>": "<KMS key ARN: platform-gate-signer output keys>", ...}}  (all six purposes)

`enroll` runs when the plan starts, before any T14 run: T19 refuses a subject that started before its
key's enrollment. `sign` signs what the relay's `subjects` shows and is not yet published, and records
each signature next to its envelope. The CLI `sign` is the operator-run step (exec into the workflow host);
the GATE stage (GateRecheck.sign) signs only under the plan's live `evidence` pre-delegation (#716, ADR-0038 3절).
"""

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_gate.contract import GateError, canonical
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key

from . import gate_relay

CONFIG = "/workflow/gate-signer.json"
PURPOSES = ("context", "run-source", "run-target", "readiness-binding", "readiness-snapshot", "gate-result")
ALGORITHM = "ED25519_SHA_512"
# ponytail: one enrollment window per plan, 48 h like the operator tool; a plan that reaches GATE later holds.
KEY_HOURS, ENVELOPE_TTL = 48, timedelta(hours=24)


class KmsKey:
    """One KMS Ed25519 key. Signs RAW messages; every signature is verified locally before it is used."""

    def __init__(self, arn, client):
        self.arn, self.client = arn, client
        info = client.get_public_key(KeyId=arn)
        if (info.get("KeySpec") != "ECC_NIST_EDWARDS25519" or info.get("KeyUsage") != "SIGN_VERIFY"
                or ALGORITHM not in info.get("SigningAlgorithms", ())):
            raise GateError("PLATFORM_SIGNER_KEY_INVALID")
        public = load_der_public_key(info["PublicKey"])
        if not isinstance(public, Ed25519PublicKey):
            raise GateError("PLATFORM_SIGNER_KEY_INVALID")
        self.public = public
        raw = public.public_bytes_raw()
        self.public_key_hex = raw.hex()
        self.key_id = "key-" + hashlib.sha256(raw).hexdigest()[:16]  # same derivation as the operator tool

    def sign(self, message):
        if len(message) > 4096:  # KMS RAW limit; T19 envelopes are a few hundred bytes
            raise GateError("PLATFORM_SIGNER_MESSAGE_TOO_LARGE")
        signature = self.client.sign(KeyId=self.arn, Message=message, MessageType="RAW",
                                     SigningAlgorithm=ALGORITHM)["Signature"]
        self.public.verify(signature, message)  # a wrong key or a corrupted answer fails here, not at GATE
        return signature


def load(config_file=CONFIG, *, client=None):
    config = json.loads(Path(config_file).read_text())
    if type(config) is not dict or set(config) != {"relay", "keys"} or type(config["keys"]) is not dict \
            or set(config["keys"]) != set(PURPOSES):
        raise ValueError("gate_signer_configuration_invalid")
    relay = gate_relay.allowed(config["relay"])
    arns = config["keys"]
    if len(set(arns.values())) != len(arns) or not all(
            isinstance(a, str) and a.split(":")[:3] == ["arn", "aws", "kms"] and len(a.split(":")) == 6
            and a.split(":")[5].startswith("key/") for a in arns.values()):
        raise ValueError("gate_signer_keys_invalid")  # one distinct key per purpose, never an alias
    if client is None:
        import boto3

        client = boto3.client("kms", region_name=next(iter(arns.values())).split(":")[3])
    return relay, {purpose: KmsKey(arn, client) for purpose, arn in arns.items()}


def enroll(call, keys, plan_id, now):
    """Enroll every purpose's key for this plan. Retries within the hour send the identical request.

    Returns (enrolled, reused): a purpose already enrolled earlier is reported as reused, not enrolled."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    out, reused = [], []
    for purpose, key in keys.items():
        try:
            call({"op": "enroll", "plan_id": plan_id, "purpose": purpose, "issuer_id": "platform-" + purpose,
                  "key_id": key.key_id, "public_key_hex": key.public_key_hex, "valid_from": start.isoformat(),
                  "valid_until": (start + timedelta(hours=KEY_HOURS)).isoformat()})
        except GateError as error:
            # Already enrolled in an earlier hour. If that record is not this key, publish fails closed.
            if error.args[:1] != ("AUTHORITY_KEY_CONFLICT",):
                raise
            reused.append(purpose)
            continue
        out.append(purpose)
    return out, reused


def sign(call, keys, plan_id, now, put_record, *, delegation=None):
    """Sign and publish what `subjects` shows and is not yet published; record the intent before each KMS call.

    `delegation(context_digest) -> delegation_id` (the GATE stage, ADR-0038) checks the plan's current
    pre-delegation before anything is recorded or signed; it raises and nothing is signed. Under it the
    platform signs measurement provenance only, never its own gate-result (#565). Nothing left to sign needs
    no delegation. Without it this is the operator-run step, and no delegation is recorded."""
    subjects = call({"op": "subjects", "plan_id": plan_id})["subjects"]
    context = subjects["context"]["digest"]
    due = [(p, context if p == "context" else subjects.get(p)) for p in PURPOSES
           if not (delegation is not None and p == "gate-result")]
    due = [(p, subject) for p, subject in due if subject is not None and p not in subjects["published"]]
    if not due:
        return []
    delegation_id = delegation(context) if delegation is not None else None
    published = []
    for purpose, subject in due:
        key = keys[purpose]
        value = AuthorityEnvelope("platform-" + purpose, key.key_id, purpose, context, subject, now.isoformat(),
                                  (now + ENVELOPE_TTL).isoformat(), "0" * 128)
        intent = ({"kind": "t19-platform-signature.v1", "purpose": purpose, "context_digest": context,
                   "subject_digest": subject, "issuer_id": value.issuer_id, "key_id": key.key_id,
                   "kms_key_arn": key.arn, "signed_at": now.isoformat()}
                  | ({"delegation_id": delegation_id} if delegation_id else {}))
        # Intent before kms:Sign; then one record per signature made, so CloudTrail Sign calls and records
        # match one to one (a retry signs a new issued_at, a crash in between leaves the intent).
        base = f"platform-signature-{purpose}-{subject[:16]}"
        put_record(plan_id, base, intent)
        signed = replace(value, signature_hex=key.sign(DOMAIN + canonical(value.payload)).hex())
        digest = hashlib.sha256(bytes.fromhex(signed.signature_hex)).hexdigest()
        put_record(plan_id, f"{base}-{digest[:16]}", intent | {"signature_sha256": digest})
        call({"op": "publish", "plan_id": plan_id, "envelope": {
            k: getattr(signed, k) for k in ("issuer_id", "key_id", "purpose", "context_digest", "subject_digest",
                                            "issued_at", "expires_at", "signature_hex")}})
        published.append(purpose)
    return published


def delegated(delegations, plan_id, version=None):
    """`sign`'s delegation check: the plan's live `evidence` delegation for this version and context.

    ApprovalDelegations.principal_for checks the plan, revocation, expiry, gate and the approver's current
    gate:publish-provenance grant, and records one use per version (a different context in the same version is
    refused). `version=None` reads it in that transaction. Every refusal is delegation_unavailable."""
    from .store import EventError

    def check(context_digest):
        try:
            principal = delegations.principal_for(plan_id, "evidence", binding={"context_digest": context_digest},
                                                  version=version)
        except EventError:
            raise EventError("delegation_unavailable") from None
        return principal.session_id.removeprefix("delegation:")

    return check


def main(argv=None):
    from . import gate_services
    from .cutover import _get, _put
    from .operator_access import OperatorAccess
    from .postgres import PostgresPlanStore
    from .store import EventError
    from .window_server import runtime_connection

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["enroll", "sign"])
    p.add_argument("--plan-id", required=True)
    args = p.parse_args(argv)
    try:
        store = PostgresPlanStore(runtime_connection())
        access = OperatorAccess(store)
        records = gate_services.gate_records(store, access)
        relay, keys = load()

        def call(request):
            return gate_relay.handle(records, request, relay, access=access)

        def put_record(plan_id, key, value):
            with store._transaction(write=True, plan_id=plan_id) as c:
                if _get(c, plan_id, key) is None:  # a retry keeps the first record
                    _put(c, plan_id, key, {"op": "platform-sign"}, value)

        now = datetime.now(UTC)
        if args.command == "enroll":
            enrolled, reused = enroll(call, keys, args.plan_id, now)
            out = {"enrolled": enrolled, "reused": reused}
        else:
            out = {"published": sign(call, keys, args.plan_id, now, put_record)}
        print(json.dumps({"status": "PASS", **out}, sort_keys=True))
    except (EventError, GateError) as error:
        code = getattr(error, "code", None) or (error.args[0] if error.args else "")
        print(json.dumps({"status": "HOLD", "code": code if isinstance(code, str) and gate_relay._CODE.fullmatch(code)
                          else type(error).__name__}))
        raise SystemExit(10) from None
    except Exception as error:  # noqa: BLE001 - one bounded code, never a trace or a KMS response
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main(sys.argv[1:])
