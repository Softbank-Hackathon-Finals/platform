"""T19 operator issuer: keys stay on this machine; the platform gets public keys and signed envelopes only.

    uv run --directory services/control-gate python ../../operations/gate/run.py <command> --context <kube context> ...

keys       create any missing per-purpose Ed25519 keys in --dir (0700 dir, 0600 files) and print public keys
subjects   print the digests to sign for the plan's current gate context, what is already published and
           `confirm`, the fingerprint of everything printed (read only)
enroll     enroll each purpose's public key for the configured issuer subject; a retry for the same plan
           reuses the validity window saved in --dir, so a partly done enrollment can be run again
record     record one T14 run document (--side source|target --run <file>)
bind       bind the initial context from the newest recorded run on each side (before any application)
evaluate   evaluate the current context; S1 records its first decision (a BLOCK) this way
readiness  record a T13 binding and snapshot (JSON files from the T13 capture) for the current context
capture    one T13 pass inside the capture's 30 s validity: the Executor observes both DBs (through the
           approval host, which holds its command credential), the relay records it for the current context
           and returns its data-readiness judgment, and this signs exactly the two readiness digests it just
           recorded. --evaluate then records the GATE decision: once per context (S1, S2), and only when the
           data judgment is PASS (a passing replication lag would otherwise cost the context; a deliberate
           BLOCK record needs --record-block). Later passes (before SHIFT, before cutover) need only the
           fresh signed record for the platform re-evaluation
sign       sign and publish what `subjects` shows and is not yet published; refuses unless --confirm matches

T19 decision (11-07): one operator issuer with one key per purpose. The workflow host only verifies.
Sign only what you reviewed: run `subjects` first and pass its `confirm` as --confirm. Anything recorded
since (a new T13 capture, a GATE result) changes it, and `sign` refuses until you look again.
`capture` is the one exception, for the readiness record only: a capture is valid 30 s, too short for a
person, so it signs the readiness-binding and readiness-snapshot digests the relay returned for the record
it just made, and refuses if `subjects` then shows anything else (a newer record, a new context).
gate-result is optional on the happy path: the workflow GATE reader consumes a platform re-evaluation.
Exit: 0 done, 10 hold (a T19/T10 refusal: read the code), 1 failed (relay, kubectl or input), 2 usage.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_gate.contract import canonical, fingerprint
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PURPOSES = ("context", "run-source", "run-target", "readiness-binding", "readiness-snapshot", "gate-result")
RELAY = ["kubectl", "--context", "{context}", "-n", "platform-state", "exec", "-i", "deploy/t10-workflow-host",
         "-c", "host", "--", "python", "-m", "bravo_platform_events.gate_relay"]
OBSERVER = ["kubectl", "--context", "{context}", "-n", "platform-state", "exec", "-i", "deploy/t15-rehearsal-host",
            "-c", "host", "--", "python", "-m", "bravo_platform_events.readiness_observer"]
READINESS = ("readiness-binding", "readiness-snapshot")
MAX_TTL = timedelta(hours=24)
MAX_KEY_HOURS = 48


class RelayFailed(Exception):
    """The relay did not answer PASS or HOLD (kubectl, timeout, crash): exit 1, never a hold."""


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


def enroll_request(plan_id, purpose, key, now, valid_hours, *, window=None):
    if not 0 < valid_hours <= MAX_KEY_HOURS:
        raise ValueError("key_validity_out_of_range")
    valid_from, valid_until = window or ((now - timedelta(seconds=1)).isoformat(),
                                         (now + timedelta(hours=valid_hours)).isoformat())
    return {"op": "enroll", "plan_id": plan_id, "purpose": purpose, "issuer_id": issuer_id(purpose),
            "key_id": key_id(key), "public_key_hex": public_hex(key),
            "valid_from": valid_from, "valid_until": valid_until}


def enroll_requests(directory, plan_id, keys, purposes, now, valid_hours):
    """One request per purpose. The first run saves each validity window in --dir; a retry for the same plan
    sends the identical request, which the server returns as already enrolled instead of a conflict."""
    path = _private_dir(directory) / f"enroll-{hashlib.sha256(plan_id.encode()).hexdigest()[:16]}.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    requests = []
    for purpose in purposes:
        if purpose not in keys:
            raise ValueError(f"key_missing:{purpose}")
        request = enroll_request(plan_id, purpose, keys[purpose], now, valid_hours, window=saved.get(purpose))
        saved[purpose] = [request["valid_from"], request["valid_until"]]
        requests.append(request)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(saved, out)
    return requests


def envelope(key, purpose, context_digest, subject_digest, now, ttl):
    """A signed AuthorityEnvelope as the relay's publish request expects it."""
    if not timedelta(0) < ttl <= MAX_TTL:
        raise ValueError("envelope_ttl_out_of_range")
    value = AuthorityEnvelope(issuer_id(purpose), key_id(key), purpose, context_digest, subject_digest,
                              now.isoformat(), (now + ttl).isoformat(), "0" * 128)
    signed = replace(value, signature_hex=key.sign(DOMAIN + canonical(value.payload)).hex())
    return {k: getattr(signed, k) for k in ("issuer_id", "key_id", "purpose", "context_digest",
                                             "subject_digest", "issued_at", "expires_at", "signature_hex")}


def sign_requests(plan_id, subjects, keys, purposes, confirm, now, ttl):
    """Publish requests for what `subjects` shows and is not yet published (or the given purposes).

    Refuses unless `confirm` is the fingerprint of everything `subjects` showed: a T13 record or a GATE
    result added after the review changes it.
    """
    shown = {k: v for k, v in subjects.items() if k not in ("confirm", "published")}
    if confirm != fingerprint(shown):
        raise ValueError("subjects_not_confirmed")  # sign only what you reviewed
    context = subjects["context"]["digest"]
    if purposes is None:
        purposes = [p for p in PURPOSES if p == "context" or p in subjects]
    requests = []
    for purpose in purposes:
        if purpose in subjects.get("published", ()):
            continue  # already published for this subject: a retry, not a new signature
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
        try:
            result = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True, timeout=120,
                                    check=False)
        except subprocess.TimeoutExpired:
            raise RelayFailed("relay_timeout") from None
        lines = result.stdout.strip().splitlines()
        try:
            answer = json.loads(lines[-1]) if lines else {}
        except ValueError:
            answer = {}
        if answer.get("status") == "PASS":
            return answer
        if answer.get("status") == "HOLD":
            raise RuntimeError(f"hold:{answer.get('code')}")
        # kubectl or the relay failed: its own last line (local terminal only), never a hold
        raise RelayFailed(answer.get("reason") or (result.stderr.strip().splitlines() or ["no_output"])[-1][:200])
    return call


def capture(args, *, relay, observe, clock):
    started = time.monotonic()
    observation = observe({})["observation"]
    recorded = relay({"op": "capture", "plan_id": args.plan_id, "observation": observation})["readiness"]
    subjects = relay({"op": "subjects", "plan_id": args.plan_id})["subjects"]
    if (subjects["context"]["digest"] != recorded["context"]
            or [subjects.get(p) for p in READINESS] != [recorded["binding"], recorded["snapshot"]]):
        raise RuntimeError("hold:readiness_record_superseded")  # not what this command recorded: sign nothing
    requests = sign_requests(args.plan_id, subjects, load_keys(args.dir), list(READINESS), subjects["confirm"],
                             clock(), timedelta(minutes=args.ttl_minutes))
    out = {"readiness": recorded, "published": [relay(r)["published"] for r in requests]}
    if args.evaluate:
        # The GATE record is written once per context: a data BLOCK (one lag spike) would cost the context.
        if recorded["data"]["decision"] != "PASS" and not getattr(args, "record_block", False):
            raise RuntimeError("hold:readiness_data_block")
        out["evaluated"] = relay({"op": "evaluate", "plan_id": args.plan_id})["evaluated"]
    out["elapsed_s"] = round(time.monotonic() - started, 1)
    return out


def run(args, *, relay, now, observe=None, clock=lambda: datetime.now(UTC)):
    if args.command == "capture":
        return capture(args, relay=relay, observe=observe, clock=clock)
    if args.command == "keys":
        keys = load_keys(args.dir, create=True)
        return {"keys": {p: {"key_id": key_id(k), "public_key_hex": public_hex(k)} for p, k in keys.items()}}
    if args.command == "subjects":
        return relay({"op": "subjects", "plan_id": args.plan_id})
    if args.command == "record":
        return relay({"op": "record", "plan_id": args.plan_id, "side": args.side,
                      "document": json.loads(Path(args.run).read_text())})
    if args.command in ("bind", "evaluate"):
        return relay({"op": args.command, "plan_id": args.plan_id})
    if args.command == "readiness":
        return relay({"op": "readiness", "plan_id": args.plan_id,
                      "binding": json.loads(Path(args.binding).read_text()),
                      "snapshot": json.loads(Path(args.snapshot).read_text())})
    keys = load_keys(args.dir)
    purposes = args.purposes.split(",") if args.purposes else None
    if args.command == "enroll":
        requests = enroll_requests(args.dir, args.plan_id, keys, purposes or list(PURPOSES), now, args.valid_hours)
        return {"enrolled": [relay(r)["enrolled"] for r in requests]}
    subjects = relay({"op": "subjects", "plan_id": args.plan_id})["subjects"]
    requests = sign_requests(args.plan_id, subjects, keys, purposes, args.confirm, now,
                             timedelta(minutes=args.ttl_minutes))
    return {"published": [relay(r)["published"] for r in requests], "already": subjects.get("published", [])}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["keys", "subjects", "enroll", "record", "bind", "evaluate", "readiness",
                                       "capture", "sign"])
    p.add_argument("--plan-id")
    p.add_argument("--context", help="kubectl context of the platform cluster")
    p.add_argument("--dir", help="private key directory (0700)")
    p.add_argument("--purposes", help=f"comma list, default all: {','.join(PURPOSES)}")
    p.add_argument("--valid-hours", type=int, default=24, help=f"key validity, at most {MAX_KEY_HOURS}")
    p.add_argument("--ttl-minutes", type=int, default=360)
    p.add_argument("--confirm", help="the `confirm` value printed by `subjects`")
    p.add_argument("--side", choices=["source", "target"])
    p.add_argument("--run", help="T14 run document (control_run JSON)")
    p.add_argument("--binding")
    p.add_argument("--snapshot")
    p.add_argument("--relay", nargs="+", help="argv reaching bravo_platform_events.gate_relay")
    p.add_argument("--observer", nargs="+", help="argv reaching bravo_platform_events.readiness_observer")
    p.add_argument("--evaluate", action="store_true", help="capture: also record the GATE decision (once per context)")
    p.add_argument("--record-block", action="store_true",
                   help="capture --evaluate: record the GATE even when the data judgment blocks")
    args = p.parse_args(argv)
    if args.command != "keys" and not (args.plan_id and (args.context or args.relay)):
        p.error("--plan-id and --context (or --relay) are required")
    if args.command in ("keys", "enroll", "sign", "capture") and not args.dir:
        p.error("--dir is required")
    if args.command == "record" and not (args.side and args.run):
        p.error("--side and --run are required")
    if args.command == "sign" and not args.confirm:
        p.error("--confirm is required: run `subjects` and review it first")
    relay = relay_with([part.format(context=args.context) for part in (args.relay or RELAY)])
    observe = relay_with([part.format(context=args.context) for part in (args.observer or OBSERVER)])
    try:
        print(json.dumps({"status": "DONE", **run(args, relay=relay, now=datetime.now(UTC), observe=observe)},
                         indent=2))
        return 0
    except RuntimeError as error:
        print(json.dumps({"status": "HOLD", "reason": str(error)[:200]}))
        return 10
    except (RelayFailed, ValueError, KeyError, OSError) as error:
        print(json.dumps({"status": "FAILED", "reason": f"{type(error).__name__}:{str(error)[:160]}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
