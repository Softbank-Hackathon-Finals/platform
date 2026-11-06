"""T19 operator issuer: keys stay on this machine; the platform gets public keys and signed envelopes only.

    uv run --directory services/control-gate python ../../operations/gate/run.py <command> --context <kube context> ...

keys       create any missing per-purpose Ed25519 keys in --dir (0700 dir, 0600 files) and print public keys
subjects   print the digests to sign for the plan's current gate context (read only)
enroll     enroll each purpose's public key for the configured issuer subject
readiness  record a T13 binding and snapshot (JSON files from the T13 capture) for the current context
sign       sign and publish envelopes for the current subjects; refuses unless --confirm-context matches

T19 decision (11-07): one operator issuer with one key per purpose. The workflow host only verifies.
Sign only what you reviewed: run `subjects` first and pass its context digest as --confirm-context.
Exit: 0 done, 10 hold (a T19/T10 refusal: read the code), 1 failed.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_gate.contract import canonical
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PURPOSES = ("context", "run-source", "run-target", "readiness-binding", "readiness-snapshot", "gate-result")
RELAY = ["kubectl", "--context", "{context}", "-n", "platform-state", "exec", "-i", "deploy/t10-workflow-host",
         "-c", "host", "--", "python", "-m", "bravo_platform_events.gate_relay"]
MAX_TTL = timedelta(hours=24)


def _private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("key_directory_must_be_private")  # 0700: no group/other access
    return path


def load_keys(directory, *, create=False):
    """{purpose: Ed25519PrivateKey} from <dir>/<purpose>.key (raw private bytes, hex)."""
    directory = _private_dir(directory)
    keys = {}
    for purpose in PURPOSES:
        path = directory / f"{purpose}.key"
        if not path.exists():
            if not create:
                continue
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(Ed25519PrivateKey.generate().private_bytes_raw().hex())
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("key_file_must_be_private")
        keys[purpose] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))
    return keys


def public_hex(key):
    return key.public_key().public_bytes_raw().hex()


def key_id(key):
    return "key-" + hashlib.sha256(bytes.fromhex(public_hex(key))).hexdigest()[:16]


def issuer_id(purpose):
    return "operator-" + purpose


def enroll_request(plan_id, purpose, key, now, valid_hours):
    return {"op": "enroll", "plan_id": plan_id, "purpose": purpose, "issuer_id": issuer_id(purpose),
            "key_id": key_id(key), "public_key_hex": public_hex(key),
            "valid_from": (now - timedelta(seconds=1)).isoformat(),
            "valid_until": (now + timedelta(hours=valid_hours)).isoformat()}


def envelope(key, purpose, context_digest, subject_digest, now, ttl):
    """A signed AuthorityEnvelope as the relay's publish request expects it."""
    if not timedelta(0) < ttl <= MAX_TTL:
        raise ValueError("envelope_ttl_out_of_range")
    value = AuthorityEnvelope(issuer_id(purpose), key_id(key), purpose, context_digest, subject_digest,
                              now.isoformat(), (now + ttl).isoformat(), "0" * 128)
    signed = replace(value, signature_hex=key.sign(DOMAIN + canonical(value.payload)).hex())
    return {k: getattr(signed, k) for k in ("issuer_id", "key_id", "purpose", "context_digest",
                                             "subject_digest", "issued_at", "expires_at", "signature_hex")}


def sign_requests(plan_id, subjects, keys, purposes, confirm_context, now, ttl):
    """Publish requests for the requested purposes present in `subjects`; refuses another context."""
    context = subjects["context"]["digest"]
    if confirm_context != context:
        raise ValueError("context_not_confirmed")  # sign only the context you reviewed
    requests = []
    for purpose in purposes:
        subject = context if purpose == "context" else subjects.get(purpose)
        if subject is None:
            raise ValueError(f"subject_missing:{purpose}")
        if purpose not in keys:
            raise ValueError(f"key_missing:{purpose}")
        requests.append({"op": "publish", "plan_id": plan_id,
                         "envelope": envelope(keys[purpose], purpose, context, subject, now, ttl)})
    return requests


def relay_with(argv):
    def call(request):
        result = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True, timeout=120,
                                check=False)
        answer = json.loads((result.stdout.strip().splitlines() or ["{}"])[-1])
        if answer.get("status") != "PASS":
            raise RuntimeError(f"{answer.get('status', 'FAIL').lower()}:{answer.get('code') or answer.get('reason')}")
        return answer
    return call


def run(args, *, relay, now):
    if args.command == "keys":
        keys = load_keys(args.dir, create=True)
        return {"keys": {p: {"key_id": key_id(k), "public_key_hex": public_hex(k)} for p, k in keys.items()}}
    if args.command == "subjects":
        return relay({"op": "subjects", "plan_id": args.plan_id})
    if args.command == "readiness":
        return relay({"op": "readiness", "plan_id": args.plan_id,
                      "binding": json.loads(Path(args.binding).read_text()),
                      "snapshot": json.loads(Path(args.snapshot).read_text())})
    keys = load_keys(args.dir)
    purposes = args.purposes.split(",") if args.purposes else list(PURPOSES)
    if args.command == "enroll":
        return {"enrolled": [relay(enroll_request(args.plan_id, p, keys[p], now, args.valid_hours))["enrolled"]
                             for p in purposes]}
    subjects = relay({"op": "subjects", "plan_id": args.plan_id})["subjects"]
    requests = sign_requests(args.plan_id, subjects, keys, purposes, args.confirm_context, now,
                             timedelta(minutes=args.ttl_minutes))
    return {"published": [relay(r)["published"] for r in requests]}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["keys", "subjects", "enroll", "readiness", "sign"])
    p.add_argument("--plan-id")
    p.add_argument("--context", help="kubectl context of the platform cluster")
    p.add_argument("--dir", help="private key directory (0700)")
    p.add_argument("--purposes", help=f"comma list, default all: {','.join(PURPOSES)}")
    p.add_argument("--valid-hours", type=int, default=24)
    p.add_argument("--ttl-minutes", type=int, default=360)
    p.add_argument("--confirm-context", help="the context digest printed by `subjects`")
    p.add_argument("--binding")
    p.add_argument("--snapshot")
    p.add_argument("--relay", nargs="+", help="argv reaching bravo_platform_events.gate_relay")
    args = p.parse_args(argv)
    relay = relay_with([part.format(context=args.context) for part in (args.relay or RELAY)])
    try:
        print(json.dumps({"status": "DONE", **run(args, relay=relay, now=datetime.now(UTC))}, indent=2))
        return 0
    except RuntimeError as error:
        print(json.dumps({"status": "HOLD", "reason": str(error)[:200]}))
        return 10
    except (ValueError, KeyError, OSError) as error:
        print(json.dumps({"status": "FAILED", "reason": f"{type(error).__name__}:{str(error)[:160]}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
