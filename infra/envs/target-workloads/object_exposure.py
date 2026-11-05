#!/usr/bin/env python3
"""Render the SeaweedFS ``s3.json`` identity file for the demo's two exposure states and
probe which state the running store is actually in (design 10 scene 0:00, T9 #13).

``closed``    bootstrap + client identities on the exact attachments bucket. Production shape.
``open-demo`` the same plus an ``anonymous`` identity with *Read only* on that one bucket.
              This is the deliberately exposed starting point of the demo so that the gate's
              IAM-01.1 question ("can an unauthenticated caller read an attachment?") answers
              BLOCK first and PASS after the fix. It never grants Write, List, Tagging or Admin,
              never touches another bucket, and must be rendered with an explicit acknowledgement.

Credentials come from the environment and are written only to the ``--output`` file (0600,
never overwritten). The summary printed to stdout contains names, actions and a file hash,
never key material. Nothing here contacts a cluster; the operator applies the Secret.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
ACCESS_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
STATES = ("closed", "open-demo")
CLIENT_ACTIONS = ("Read", "Write", "List", "Tagging")
BOOTSTRAP_ACTIONS = ("Admin",) + CLIENT_ACTIONS
ANONYMOUS_ACTIONS = ("Read",)
ENV = {
    "bootstrap": ("BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY", "BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY"),
    "client": ("BRAVO_OBJECT_CLIENT_ACCESS_KEY", "BRAVO_OBJECT_CLIENT_SECRET_KEY"),
}


def _bucket(value: str) -> str:
    if not BUCKET_RE.fullmatch(value) or "*" in value or ".." in value:
        raise ValueError("exact lowercase bucket name required")
    return value


def _credentials(environ) -> dict[str, dict[str, str]]:
    out = {}
    for name, (access_var, secret_var) in ENV.items():
        access, secret = environ.get(access_var, ""), environ.get(secret_var, "")
        if not ACCESS_KEY_RE.fullmatch(access) or len(secret) < 32 or any(c.isspace() for c in secret):
            raise ValueError(f"{name} credentials missing or malformed in environment")
        out[name] = {"accessKey": access, "secretKey": secret}
    if out["bootstrap"]["accessKey"] == out["client"]["accessKey"]:
        raise ValueError("bootstrap and client must be distinct identities")
    return out


def render(state: str, bucket: str, environ, *, acknowledge_open: bool = False, demo_run_id: str = "") -> dict:
    """Return the ``s3.json`` document. ``open-demo`` needs both an acknowledgement and a run id."""
    if state not in STATES:
        raise ValueError("unknown exposure state")
    bucket = _bucket(bucket)
    creds = _credentials(environ)
    identities = [
        {"name": "bootstrap", "credentials": [creds["bootstrap"]], "actions": [f"{a}:{bucket}" for a in BOOTSTRAP_ACTIONS]},
        {"name": "client", "credentials": [creds["client"]], "actions": [f"{a}:{bucket}" for a in CLIENT_ACTIONS]},
    ]
    if state == "open-demo":
        if not acknowledge_open or not re.fullmatch(r"[a-z0-9][a-z0-9-]{3,63}", demo_run_id or ""):
            raise ValueError("open-demo requires --acknowledge-anonymous-read and a demo run id")
        identities.append({"name": "anonymous", "actions": [f"{a}:{bucket}" for a in ANONYMOUS_ACTIONS]})
    return {"identities": identities}


def summary(doc: dict, state: str, bucket: str, data: bytes, demo_run_id: str = "") -> dict:
    anonymous = next((i for i in doc["identities"] if i["name"] == "anonymous"), None)
    return {
        "state": state, "bucket": bucket, "identities": [i["name"] for i in doc["identities"]],
        "anonymous_actions": anonymous["actions"] if anonymous else [],
        "demo_run_id": demo_run_id or None, "s3_json_sha256": hashlib.sha256(data).hexdigest(),
        "credentials_in_output": False,
    }


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def probe(endpoint: str, bucket: str, key: str, *, opener=urllib.request.urlopen, timeout: float = 10.0) -> dict:
    """Unsigned GET of one object. 200 → the bucket is open to anonymous readers."""
    bucket = _bucket(bucket)
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?", endpoint):
        raise ValueError("endpoint must be scheme://host[:port] with no path")
    if not key or key.startswith("/") or ".." in key.split("/") or len(key) > 512:
        raise ValueError("object key outside policy")
    request = urllib.request.Request(f"{endpoint}/{bucket}/{key}", method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            response.read(1)  # confirm the body is served, never retain it
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return {"anonymous_get": f"UNKNOWN:{type(error).__name__}", "state_observed": "unknown", "key_suffix": key.rsplit("/", 1)[-1]}
    outcome = "ALLOWED" if status == 200 else "DENIED" if status in (401, 403) else f"UNKNOWN:{status}"
    observed = {"ALLOWED": "open", "DENIED": "closed"}.get(outcome, "unknown")
    return {"anonymous_get": outcome, "state_observed": observed, "key_suffix": key.rsplit("/", 1)[-1]}


def plan(state: str, bucket: str) -> dict:
    """Operator sequence to put the running store into ``state``; printed, never executed here."""
    _bucket(bucket)
    if state not in STATES:
        raise ValueError("unknown exposure state")
    return {"state": state, "steps": [
        f"object_exposure.py render --state {state} --bucket {bucket} --output /dev/shm/s3.json (keys from env)",
        "kubectl -n target-dev create secret generic target-seaweedfs-auth --from-file=s3.json=/dev/shm/s3.json --dry-run=client -o json | kubectl apply -f -",
        "kubectl -n target-dev rollout restart deployment/target-seaweedfs && kubectl -n target-dev rollout status deployment/target-seaweedfs",
        "shred -u /dev/shm/s3.json",
        f"object_exposure.py probe --endpoint http://seaweedfs.target-dev.svc.cluster.local:8333 --bucket {bucket} --key <existing object key>  → expect state_observed={'open' if state == 'open-demo' else 'closed'}",
        "run the gate (IAM-01.1) and keep its readback as the evidence for this state",
    ], "note": "open-demo is a demo fixture state only; return to closed before any cutover work"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render", help="write s3.json for a state; prints a summary without secrets")
    r.add_argument("--state", choices=STATES, required=True)
    r.add_argument("--bucket", default="demo-chat-attachments")
    r.add_argument("--output", required=True, help="new file path (0600); refuses to overwrite")
    r.add_argument("--acknowledge-anonymous-read", action="store_true")
    r.add_argument("--demo-run-id", default="")
    p = sub.add_parser("probe", help="unsigned GET to observe whether the bucket is open")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--bucket", default="demo-chat-attachments")
    p.add_argument("--key", required=True)
    q = sub.add_parser("plan", help="print the operator steps for a state")
    q.add_argument("--state", choices=STATES, required=True)
    q.add_argument("--bucket", default="demo-chat-attachments")
    args = parser.parse_args(argv)
    if args.command == "render":
        doc = render(args.state, args.bucket, os.environ, acknowledge_open=args.acknowledge_anonymous_read, demo_run_id=args.demo_run_id)
        data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
        write_private(Path(args.output), data)
        print(json.dumps(summary(doc, args.state, args.bucket, data, args.demo_run_id)))
        return 0
    if args.command == "probe":
        result = probe(args.endpoint, args.bucket, args.key)
        print(json.dumps(result))
        return 0 if result["state_observed"] != "unknown" else 1
    print(json.dumps(plan(args.state, args.bucket), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(2) from None
