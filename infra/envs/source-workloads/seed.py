#!/usr/bin/env python3
"""Seed new development credentials through process memory and stdin only.

Existing owned values are reused; differing AWS values are never overwritten.
No secret value, SQL password, provider error or command output is printed.
"""

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import subprocess
from pathlib import Path

NAMESPACE = "source-dev"
OWNER = "bravo-source"


def scram(password, salt=None):
    salt = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    client = hmac.digest(salted, b"Client Key", "sha256")
    stored = hashlib.sha256(client).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")
    def enc(value):
        return base64.b64encode(value).decode()
    return f"SCRAM-SHA-256$4096:{enc(salt)}${enc(stored)}:{enc(server)}"


def password_matches(password, verifier):
    try:
        algorithm, params, keys = verifier.split("$")
        iterations, salt = params.split(":")
        stored, server = keys.split(":")
        if algorithm != "SCRAM-SHA-256" or not 4096 <= int(iterations) <= 1000000:
            return False
        salted = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(base64.b64decode(stored), hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()) and hmac.compare_digest(base64.b64decode(server), hmac.digest(salted, b"Server Key", "sha256"))
    except (ValueError, TypeError):
        return False


class Runtime:
    def __init__(self, kubeconfig):
        self.kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--namespace", NAMESPACE]

    @staticmethod
    def call(command, payload=None):
        result = subprocess.run(command, input=payload, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise RuntimeError("provider operation failed; raw output suppressed")
        return result.stdout

    def secret(self, name, create=None):
        raw = self.call(self.kube + ["get", "secret", name, "--ignore-not-found", "-o", "json"])
        if raw.strip():
            obj = json.loads(raw)
            if obj["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != OWNER:
                raise RuntimeError("existing secret belongs to another owner")
            return {k: base64.b64decode(v).decode() for k, v in obj.get("data", {}).items()}
        if create is None:
            raise RuntimeError("required credential has not been seeded")
        values = create()
        obj = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
               "metadata": {"name": name, "namespace": NAMESPACE,
                            "labels": {"app.kubernetes.io/part-of": OWNER, "component": "source"}},
               "stringData": values}
        # create (not apply): no plaintext last-applied annotation.
        self.call(self.kube + ["create", "-f", "-"], json.dumps(obj))
        return values

    def db(self):
        for name, user in [("source-app-db", "chat_app"), ("source-replication", "chat_replicator")]:
            value = self.secret(name, lambda user=user: {"username": user, "password": secrets.token_urlsafe(36)})
            if value.get("username") != user or not value.get("password"):
                raise RuntimeError("existing database credential shape mismatch")

    def pooler(self):
        cluster = json.loads(self.call(self.kube + ["get", "cluster", "source-db", "-o", "json"]))
        primary = cluster.get("status", {}).get("currentPrimary")
        if not primary:
            raise RuntimeError("database primary is not ready")
        verifier = self.call(self.kube + ["exec", primary, "-c", "postgres", "--", "psql", "-XAt", "-U", "postgres", "-d", "chat", "-c",
            "SELECT rolpassword FROM pg_catalog.pg_authid WHERE rolname = 'chat_app'"]).strip()
        app = self.secret("source-app-db")
        if not password_matches(app["password"], verifier):
            raise RuntimeError("CNPG has not reconciled the exact application SCRAM verifier")
        control = self.secret("source-control-bootstrap", lambda: {
            "admin_token": secrets.token_urlsafe(36), "pgbouncer_password": secrets.token_urlsafe(36)})
        expected = {"userlist.txt": f'"chat_app" "{verifier}"\n"cutover_control" "{scram(control["pgbouncer_password"])}"\n'}
        actual = self.secret("source-pgbouncer-auth", lambda: expected)
        # The control verifier has a random salt; validate its password, not bytes.
        lines = actual.get("userlist.txt", "").splitlines()
        if len(lines) != 2 or lines[0] != expected["userlist.txt"].splitlines()[0]:
            raise RuntimeError("existing pooler application verifier differs; rotate deliberately")
        if not lines[1].startswith('"cutover_control" "') or not password_matches(control["pgbouncer_password"], lines[1].split('"')[3]):
            raise RuntimeError("existing pooler control credential differs")

    def publish(self, metadata, client=None):
        if client is None:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError

            client = boto3.client("secretsmanager", region_name=metadata["region"])
            try:
                return self.publish(metadata, client)
            except (BotoCoreError, ClientError):
                raise RuntimeError("Secrets Manager operation failed; raw output suppressed") from None
        pairs = [(metadata["db_secret_id"], self.secret("source-app-db")),
                 (metadata["control_secret_id"], self.secret("source-control-bootstrap"))]
        for name, desired in pairs:
            status = client.describe_secret(SecretId=name)
            if any("AWSCURRENT" in stages for stages in status.get("VersionIdsToStages", {}).values()):
                existing = client.get_secret_value(SecretId=name)
                if json.loads(existing["SecretString"]) != desired:
                    raise RuntimeError("AWS secret already contains different data; overwrite refused")
            else:
                client.put_secret_value(SecretId=name, SecretString=json.dumps(desired))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["db", "pooler", "publish"])
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    runtime = Runtime(args.kubeconfig)
    try:
        if args.step == "publish":
            if not args.metadata:
                parser.error("publish requires --metadata")
            runtime.publish(json.loads(args.metadata.read_text()))
        else:
            getattr(runtime, args.step)()
    except (RuntimeError, ValueError, KeyError, OSError, ImportError, subprocess.TimeoutExpired):
        print(json.dumps({"step": args.step, "result": "BLOCK", "secret_values_logged": False}))
        raise SystemExit(1) from None
    print(json.dumps({"step": args.step, "result": "PASS", "secret_values_logged": False}))


if __name__ == "__main__":
    main()
