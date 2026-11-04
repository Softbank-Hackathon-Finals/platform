"""Operator-only first issuance; identity metadata on stdin, no secret output.

Run in an authenticated kubectl context. The receiver gets only the hash and
server-owned identity; the plaintext token is a separate Secret for the agreed
producer delivery process. Existing Secrets are never overwritten. Activate the
binding with the trusted WindowIntake.set_binding port only after verifying the
plan and producer owner. This command does not grant database authority.
"""

import hashlib
import json
import secrets
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from bravo_platform_events.window_store import ProducerIdentity


def main():
    identity = ProducerIdentity(**json.load(sys.stdin))
    if not datetime.now(UTC) < datetime.fromisoformat(identity.expires_at) <= datetime.now(UTC) + timedelta(hours=8):
        raise ValueError("credential_expiry_must_be_within_eight_hours")
    names = ("t10-window-producer-identity", "t10-window-producer-token")
    for name in names:
        check = subprocess.run(["kubectl", "get", "secret", name, "-n", "platform-state",
                                "--ignore-not-found", "-o", "name"], capture_output=True, check=True)
        if check.stdout.strip():
            raise ValueError("credential_already_exists_revoke_and_rotate_explicitly")
    token = secrets.token_urlsafe(48)
    payloads = ({"producer.json": json.dumps({"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                            "identity": asdict(identity)})}, {"token": token})
    for name, payload in zip(names, payloads, strict=True):
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": name, "namespace": "platform-state", "labels": {"component": "platform"}},
                  "stringData": payload}
        result = subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(secret).encode(), capture_output=True)
        if result.returncode:
            # A partial creation cannot authenticate a request without a DB binding.
            raise RuntimeError("credential_creation_incomplete_inspect_secret_names")
    print(json.dumps({"created": list(names), "binding_activated": False, "credential_values_emitted": False}))


if __name__ == "__main__":
    main()
